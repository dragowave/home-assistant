"""
Support for Minut Point.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/point/
"""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect, async_dispatcher_send)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import HomeAssistantType
from homeassistant.util.dt import as_local, parse_datetime, utc_from_timestamp

from . import config_flow  # noqa  pylint_disable=unused-import
from .const import (
    CONF_WEBHOOK_URL, DOMAIN, EVENT_RECEIVED, NEW_DEVICE, SCAN_INTERVAL,
    SIGNAL_UPDATE_ENTITY, SIGNAL_WEBHOOK)

REQUIREMENTS = ['pypoint==1.0.5']
DEPENDENCIES = ['webhook']

_LOGGER = logging.getLogger(__name__)

CONF_CLIENT_ID = 'client_id'
CONF_CLIENT_SECRET = 'client_secret'

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN:
        vol.Schema({
            vol.Required(CONF_CLIENT_ID): cv.string,
            vol.Required(CONF_CLIENT_SECRET): cv.string,
        })
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Set up the Minut Point component."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]

    config_flow.register_flow_implementation(
        hass, DOMAIN, conf[CONF_CLIENT_ID],
        conf[CONF_CLIENT_SECRET])

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={'source': config_entries.SOURCE_IMPORT},
        ))

    return True


async def async_setup_entry(hass: HomeAssistantType, entry: ConfigEntry):
    """Set up Point from a config entry."""
    from pypoint import PointSession

    def token_saver(token):
        _LOGGER.debug('Saving updated token')
        entry.data[CONF_TOKEN] = token
        hass.config_entries.async_update_entry(entry, data={**entry.data})

    # Force token update.
    entry.data[CONF_TOKEN]['expires_in'] = -1
    session = PointSession(
        entry.data['refresh_args']['client_id'],
        token=entry.data[CONF_TOKEN],
        auto_refresh_kwargs=entry.data['refresh_args'],
        token_saver=token_saver,
    )

    if not session.is_authorized:
        _LOGGER.error('Authentication Error')
        return False

    await async_setup_webhook(hass, entry, session)
    client = MinutPointClient(hass, entry, session)
    hass.data.setdefault(DOMAIN, {}).update({entry.entry_id: client})
    await client.update()

    return True


async def async_setup_webhook(hass: HomeAssistantType, entry: ConfigEntry,
                              session):
    """Set up a webhook to handle binary sensor events."""
    if CONF_WEBHOOK_ID not in entry.data:
        entry.data[CONF_WEBHOOK_ID] = \
            hass.components.webhook.async_generate_id()
        entry.data[CONF_WEBHOOK_URL] = \
            hass.components.webhook.async_generate_url(
                entry.data[CONF_WEBHOOK_ID])
        _LOGGER.info('Registering new webhook at: %s',
                     entry.data[CONF_WEBHOOK_URL])
        hass.config_entries.async_update_entry(
            entry, data={
                **entry.data,
            })
    session.update_webhook(entry.data[CONF_WEBHOOK_URL],
                           entry.data[CONF_WEBHOOK_ID])

    hass.components.webhook.async_register(entry.data[CONF_WEBHOOK_ID],
                                           handle_webhook)


async def async_unload_entry(hass: HomeAssistantType, entry: ConfigEntry):
    """Unload a config entry."""
    hass.components.webhook.async_unregister(entry.data[CONF_WEBHOOK_ID])
    client = hass.data[DOMAIN].pop(entry.entry_id)
    client.remove_webhook()

    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)

    for component in ('binary_sensor', 'sensor'):
        await hass.config_entries.async_forward_entry_unload(
            entry, component)

    return True


async def handle_webhook(hass, webhook_id, request):
    """Handle webhook callback."""
    try:
        data = await request.json()
        _LOGGER.debug("Webhook %s: %s", webhook_id, data)
    except ValueError:
        return None

    if isinstance(data, dict):
        data['webhook_id'] = webhook_id
        async_dispatcher_send(hass, SIGNAL_WEBHOOK, data, data.get('hook_id'))
    hass.bus.async_fire(EVENT_RECEIVED, data)


class MinutPointClient():
    """Get the latest data and update the states."""

    def __init__(self, hass: HomeAssistantType, config_entry: ConfigEntry,
                 session):
        """Initialize the Minut data object."""
        self._known_devices = []
        self._hass = hass
        self._config_entry = config_entry
        self._is_available = True
        self._client = session

        async_track_time_interval(self._hass, self.update, SCAN_INTERVAL)

    async def update(self, *args):
        """Periodically poll the cloud for current state."""
        await self._sync()

    async def _sync(self):
        """Update local list of devices."""
        if not self._client.update() and self._is_available:
            self._is_available = False
            _LOGGER.warning("Device is unavailable")
            return

        self._is_available = True
        for device in self._client.devices:
            if device.device_id not in self._known_devices:
                # A way to communicate the device_id to entry_setup,
                # can this be done nicer?
                self._config_entry.data[NEW_DEVICE] = device.device_id
                await self._hass.config_entries.async_forward_entry_setup(
                    self._config_entry, 'sensor')
                await self._hass.config_entries.async_forward_entry_setup(
                    self._config_entry, 'binary_sensor')
                self._known_devices.append(device.device_id)
                del self._config_entry.data[NEW_DEVICE]
        async_dispatcher_send(self._hass, SIGNAL_UPDATE_ENTITY)

    def device(self, device_id):
        """Return device representation."""
        return self._client.device(device_id)

    def is_available(self, device_id):
        """Return device availability."""
        return device_id in self._client.device_ids

    def remove_webhook(self):
        """Remove the session webhook."""
        return self._client.remove_webhook()


class MinutPointEntity(Entity):
    """Base Entity used by the sensors."""

    def __init__(self, point_client, device_id, device_class):
        """Initialize the entity."""
        self._async_unsub_dispatcher_connect = None
        self._client = point_client
        self._id = device_id
        self._name = self.device.name
        self._device_class = device_class
        self._updated = utc_from_timestamp(0)
        self._value = None

    def __str__(self):
        """Return string representation of device."""
        return "MinutPoint {}".format(self.name)

    async def async_added_to_hass(self):
        """Call when entity is added to hass."""
        _LOGGER.debug('Created device %s', self)
        self._async_unsub_dispatcher_connect = async_dispatcher_connect(
            self.hass, SIGNAL_UPDATE_ENTITY, self._update_callback)
        self._update_callback()

    async def async_will_remove_from_hass(self):
        """Disconnect dispatcher listener when removed."""
        if self._async_unsub_dispatcher_connect:
            self._async_unsub_dispatcher_connect()

    @callback
    def _update_callback(self):
        """Update the value of the sensor."""
        pass

    @property
    def available(self):
        """Return true if device is not offline."""
        return self._client.is_available(self.device_id)

    @property
    def device(self):
        """Return the representation of the device."""
        return self._client.device(self.device_id)

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def device_id(self):
        """Return the id of the device."""
        return self._id

    @property
    def device_state_attributes(self):
        """Return status of device."""
        attrs = self.device.device_status
        attrs['last_heard_from'] = \
            as_local(self.last_update).strftime("%Y-%m-%d %H:%M:%S")
        return attrs

    @property
    def device_info(self):
        """Return a device description for device registry."""
        device = self.device.device
        return {
            'connections': {('mac', device['device_mac'])},
            'identifieres': device['device_id'],
            'manufacturer': 'Minut',
            'model': 'Point v{}'.format(device['hardware_version']),
            'name': device['description'],
            'sw_version': device['firmware']['installed'],
        }

    @property
    def name(self):
        """Return the display name of this device."""
        return "{} {}".format(self._name, self.device_class.capitalize())

    @property
    def is_updated(self):
        """Return true if sensor have been updated."""
        return self.last_update > self._updated

    @property
    def last_update(self):
        """Return the last_update time for the device."""
        last_update = parse_datetime(self.device.last_update)
        return last_update

    @property
    def should_poll(self):
        """No polling needed for point."""
        return False

    @property
    def unique_id(self):
        """Return the unique id of the sensor."""
        return 'point.{}-{}'.format(self._id, self.device_class)

    @property
    def value(self):
        """Return the sensor value."""
        return self._value
