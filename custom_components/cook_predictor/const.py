"""Constants for the Cook Predictor integration."""

from typing import Final

DOMAIN: Final = "cook_predictor"

SUBENTRY_CHANNEL: Final = "channel"

# Channel subentry data. The source entity and the role are fixed for the channel's life:
# another sensor is another channel, so a recorded series never changes meaning.
CONF_KEY: Final = "key"
CONF_ENTITY_ID: Final = "entity_id"
CONF_SOURCE_UID: Final = "source_uid"
CONF_ROLE: Final = "role"
CONF_NAME: Final = "name"
CONF_AMBIENT: Final = "ambient"
CONF_CONNECTION: Final = "connection"
CONF_DECODER: Final = "decoder"

ROLE_INTERNAL: Final = "internal"
ROLE_AMBIENT: Final = "ambient"
ROLES: Final = (ROLE_INTERNAL, ROLE_AMBIENT)

# An internal channel's pit: the only ambient channel when there is exactly one, a chosen
# ambient channel's key, or none (the trend alone, no Newton model).
AMBIENT_AUTO: Final = "auto"
AMBIENT_NONE: Final = "none"

# How a source reports a disconnected probe. Generic sources only have numbers and gaps;
# the Inkbird IBBQ series (tuya_local) reads 3623 °C (6553.5 °F) with no probe plugged in.
DECODER_GENERIC: Final = "generic"
DECODER_INKBIRD: Final = "inkbird"
DECODERS: Final = (DECODER_GENERIC, DECODER_INKBIRD)

# Config entry options.
CONF_NOTIFY: Final = "notify"
CONF_NOTIFY_ENTITIES: Final = "notify_entities"
CONF_CLICK_PATH: Final = "click_path"

DEFAULT_TITLE: Final = "Cook"

# Setting defaults for a new internal channel.
DEFAULT_TARGET_C: Final = 74.0
DEFAULT_WARN_MIN: Final = 30.0
DEFAULT_ALERT_TEMP_C: Final = 60.0
DEFAULT_ALERT_MIN: Final = 0.0

STORAGE_DIR: Final = "cook_predictor"
