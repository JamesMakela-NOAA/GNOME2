"""
util.py: Utility function for the webgnome package.
"""
import argparse
import errno
import colander
import datetime
import inspect
import json
import logging
import posixpath
import math
import os
import shutil
import sys
import time
import uuid

from functools import wraps
from itertools import chain
from pyramid.exceptions import Forbidden
from pyramid.renderers import JSON
from hazpy.unit_conversion.unit_data import ConvertDataUnits


logger = logging.getLogger(__file__)


# The format string used in :fn:`datetime.datetime.strptime` to parse
# datetimes generated by :fn:`datetime.datetime.isoformat`.
DATETIME_ISOFORMAT = '%Y-%m-%dT%H:%M:%SZ'


def make_message(type_in, text):
    """
    Create a dictionary suitable to be returned in a JSON response as a
    "message" sent to the JavaScript client.

    The client looks for "message" objects included in JSON responses that the
    server sends back on successful form submits and if one is present, and has
    a ``type`` field and ``text`` field, it will display the message to the
    user.
    """
    return dict(type=type_in, text=text)


def encode_json_date(obj):
    """
    Render a :class:`datetime.datetime` or :class:`datetime.date` object using
    the :meth:`datetime.isoformat` function, so it can be properly serialized to
    JSON notation.
    """
    if isinstance(obj, datetime.datetime) or isinstance(obj, datetime.date):
        return obj.isoformat()


def json_encoder(obj):
    """
    A custom JSON encoder that handles :class:`datetime.datetime` and
    :class:`datetime.date` values, with a fallback to using the :func:`str`
    representation of an object.
    """
    date_str = encode_json_date(obj)

    if date_str:
        return date_str
    elif obj is colander.null:
        return None
    else:
        return str(obj)


def json_date_adapter(obj, request):
    """
    A wrapper around :func:`json_date_encoder` so that it may be used in a
    custom JSON adapter for a Pyramid renderer.
    """
    return encode_json_date(obj)


gnome_json = JSON(adapters=(
    (datetime.datetime, json_date_adapter),
    (datetime.date, json_date_adapter),
    (uuid.UUID, lambda obj, request: str(obj))
))


def to_json(obj, encoder=json_encoder):
    return json.dumps(obj, default=encoder)


class SchemaForm(object):
    """
    A class that creates fields on itself based on a Colander schema.

    Instances are given fields of the same name as fields on the schema. Values
    for the fields are set by looking up same-named fields or dict keys in an
    ``obj`` passed into the constructor.

    If not passed both an object and a schema, the form will use any defaults
    the schema provides for field values.
    """
    class ObjectValue(object):
        def __init__(self, fields):
            for field in fields:
                self.__dict__[field[0]] = field[1]

        def __repr__(self):
            return 'ObjectValue(%s)' % (
            ','.join(['%s=%s' % (k, v) for k, v in self.__dict__.items()]))

    def __init__(self, schema, obj=None):
        self.schema = schema().bind()
        self.obj = obj
        self._fields = {}
        self.create_fields()

    def __getattr__(self, name):
        if name in self._fields:
            return self._fields[name]
        else:
            raise AttributeError(name)

    def __get__(self, name):
        return self._fields[name]

    def get_default_field_value(self, field):
        value = field.default

        if value is colander.null and field.children:
            fields = []
            for sub_field in field.children:
                sub_field_value = self.get_default_field_value(sub_field)
                fields.append((sub_field.name, sub_field_value))
            value = dict(fields)

        return value

    def make_value_object(self, value):
        fields = []
        for key, val in value.items():
            if isinstance(val, dict):
                val = self.make_value_object(val)
            fields.append((key, val))
        return self.ObjectValue(fields)

    def get_field_value(self, field, target=None):
        """
        Return a value for a Colander field object ``field``.

        If ``target`` is provided and is an object or dictionary, then
        attempt to look up the value by the name of ``field`` on ``target``.

        Otherwise, or if the value was not found on ``target``, use the field's
        default value if provided, falling back to None.
        """
        if isinstance(target, dict):
            value = target.get(field.name, None)
        else:
            value = getattr(target, field.name, None)

        if value is None:
            value = self.get_default_field_value(field)
        else:
            value = field.serialize(value)

        if isinstance(value, dict):
            value = self.make_value_object(value)

        return value

    def create_fields(self):
        """
        Create a field on self for each field in the given Colander schema.

        If ``obj`` was given in the constructor, use any value found for a
        field by looking it up by name on ``obj``, either as a field or a key
        in a dict-like object.

        If ``obj`` was not given, look up field defaults.
        """
        for field in self.schema.children:
            self._fields[field.name] = self.get_field_value(field, self.obj)


def get_model_from_session(request):
    """
    Return a :class:`gnome.model.Model` if the user has a session key that
    matches the ID of a running model.
    """
    settings = request.registry.settings
    model_id = request.session.get(settings.model_session_key, None)

    try:
        model = settings.Model.get(model_id)
    except settings.Model.DoesNotExist:
        model = None

    return model


MISSING_MODEL_ERROR = {
    'error': True,
    'message': make_message('error', 'That model is no longer available.')
}


def valid_model_id(request):
    """
    A Cornice validator that returns a 404 if a valid model was not found
    in the user's session.
    """
    model = None
    model_id = request.matchdict.get('model_id', None)
    Model = request.registry.settings.Model

    if model_id:
        try:
            model = Model.get(model_id)
        except Model.DoesNotExist:
            model = None

    if model is None:
        request.errors.add('body', 'model', 'Model not found.')
        request.errors.status = 404
        return

    authenticated_model_id = request.session.get(
        request.registry.settings['model_session_key'], None)

    if model.id != authenticated_model_id:
        raise Forbidden()

    request.validated['model'] = model


def valid_wind_id(request):
    """
    A Cornice validator that tests if a JSON representation of a
    :class:`model_manager.WebWindMover` includes a `wind_id` value that refers
    to a :class`model_manager.WebWind` value that exists on the current model.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']

    try:
        wind_id = request.validated['wind_id']
    except KeyError:
        request.errors.add('body', 'wind_mover', 'Missing "wind_id" value.')
        request.errors.status = 400
        return

    try:
        request.validated['wind'] = model.environment[wind_id]
    except KeyError:
        request.errors.add(
            'body', 'wind_mover', 'The wind_id did not match an existing Wind.')
        request.errors.status = 400

    return


def valid_map(request):
    """
    A Cornice validator that returns a 404 if a map was not found for the user's
    current model.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']

    if not model.map:
        request.errors.add('body', 'map', 'Map not found.')
        request.errors.status = 404


def valid_location_file(request):
    location_name = request.matchdict['location']
    settings = request.registry.settings
    location = settings.location_file_data.get(location_name, None)

    def not_found():
        request.errors.add('body', 'location_file', 'Location file not found.')
        request.errors.status = 404

    if not location:
        return not_found()

    data_dir = os.path.join(settings.location_file_dir, location_name)
    location_file = os.path.join(data_dir, 'location.json')

    if not os.path.exists(location_file):
        return not_found()

    with open(location_file) as f:
        try:
            data = json.loads(f.read())
        except (TypeError, OSError) as e:
            request.errors.add('body', 'location_file',
                               'Could not open location file')
            request.errors.status = 500
            logger.exception(e)
            return

    request.validated['location_file_data'] = location
    request.validated['location_file_model_data'] = data
    request.validated['location_dir'] = data_dir


def valid_location_file_wizard(request):
    location = request.matchdict['location']
    location_handlers = request.registry.settings.get('location_handlers')
    location_data = request.registry.settings.get('location_file_data')
    handler = location_handlers.get(location, None)
    location_data = location_data.get(location, {})
    wizard_html = location_data.get('wizard_html', None)
    wizard_json = location_data.get('wizard_json', '')

    if not wizard_html or not handler or not hasattr(handler, '__call__'):
        request.errors.add('body', 'location_file_wizard',
                           'An input handler for the wizard was not found.')
        request.errors.status = 404
        return

    request.validated['wizard_handler'] = handler
    request.validated['wizard_html'] = wizard_html
    request.validated['wizard_json'] = wizard_json


def valid_new_location_file(request):
    data_dir = os.path.join(request.registry.settings.location_file_dir,
                            request.matchdict['location'])

    if os.path.exists(data_dir):
        request.errors.add('body', 'location_file',
                           'Location file already exists.')
        request.errors.status = 400
        return

    request.validated['location_dir'] = data_dir


def valid_filename(request):
    """
    A Cornice validator that verifies that a 'filename' in ``request``
    can be used safely to upload a file into the model's data directory.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']
    filename = request.POST['filename'].filename

    if not filename:
        request.errors.add('body', 'filename', 'Illegal filename.')
        request.errors.status = 400
        return

    request.validated['filename'] = safe_join(model.base_dir, filename)


def valid_uploaded_file(request):
    """
    A Cornice validator that verifies that a 'filename' specified in ``request``
    exists in the data directory of the user's current model.

    Once validated, the absolute path of the file will be added to the
    `request.validated` dictionary.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']
    filename = request.validated['filename']
    abs_filename = os.path.join(model.static_data_dir, filename)
    relative_filename = os.path.join('data', filename)

    if not os.path.exists(abs_filename):
        request.errors.add('body', 'filename', 'File does not exist.')
        request.errors.status = 400
        return

    request.validated['filename'] = relative_filename


def valid_environment_id(request):
    """
    A Cornice validator that returns a 404 if a valid environment object was
    not found using an ``id`` matchdict value.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']

    if not request.matchdict['id'] in model.environment:
        request.errors.add('body', 'environment',
                           'Environment object not found.')
        request.errors.status = 404


def valid_mover_id(request):
    """
    A Cornice validator that returns a 404 if a valid mover was not found using
    an ``id`` matchdict value.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']

    if not request.matchdict['id'] in model.movers:
        request.errors.add('body', 'mover', 'Mover not found.')
        request.errors.status = 404


def valid_spill_id(request):
    """
    A Cornice validator that returns a 404 if a valid spill was not found using
    an ``id`` matchdict value.
    """
    valid_model_id(request)

    if request.errors:
        return

    model = request.validated['model']
    try:
        spill = model.spills[request.matchdict['id']]
    except KeyError:
        request.errors.add('body', 'spill', 'Spill not found.')
        request.errors.status = 404


def valid_coordinate_pair(request):
    """
    A Cornice validator that looks for a coordinate pair sent as `lat` and
    `lon` GET parameters.
    """
    lat = request.GET.get('lat', None)
    lon = request.GET.get('lon', None)

    if lat is None:
        request.errors.add('body', 'coordinates', 'Latitude is required as '
                                                  '"lat" GET parameter.')
        request.errors.status = 400

    if lon is None:
        request.errors.add('body', 'coordinates', 'Longitude is required as '
                                                  '"lon" GET parameter.')
        request.errors.status = 400

    request.validated['coordinates'] = {
        'lat': lat,
        'lon': lon
    }


def require_model(f):
    """
    Wrap a JSON view in a precondition that ensures the user has a valid model
    ID in his or her session.

    If the key is missing or no model is found for that key, create a new model.

    This decorator works on functions and methods. It returns a method decorator
    if the first argument to the function is ``self``. Otherwise, it returns a
    function decorator.
    """
    args = inspect.getargspec(f)

    if args and args.args[0] == 'self':
        @wraps(f)
        def inner_method(self, *args, **kwargs):
            model = get_model_from_session(self.request)
            settings = self.request.registry.settings
            if model is None:
                model = settings.Model.create()
            return f(self, model, *args, **kwargs)
        wrapper = inner_method
    else:
        @wraps(f)
        def inner_fn(request, *args, **kwargs):
            model = get_model_from_session(request)
            settings = request.registry.settings
            if model is None:
                model = settings.Model.create()
            return f(request, model, *args, **kwargs)
        wrapper = inner_fn
    return wrapper


def get_obj_class(obj):
    return obj if type(obj) == type else obj.__class__


class DirectionConverter(object):
    DIRECTIONS = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW"
    ]

    @classmethod
    def is_cardinal_direction(cls, direction):
        return direction in cls.DIRECTIONS

    @classmethod
    def get_cardinal_name(cls, degree):
        """
        Convert an integer degree into a cardinal direction name.
        """
        idx = int(math.floor((+(degree) + 360 / 32) / (360 / 16) % 16))
        if idx:
            return cls.DIRECTIONS[idx]

    @classmethod
    def get_degree(cls, cardinal_direction):
        """
       Convert a cardinal direction name into an integer degree.
       """
        idx = cls.DIRECTIONS.index(cardinal_direction.upper())
        if idx:
            return (360.0 / 16) * idx


def get_model_image_url(request, model, filename):
    """
    Get the URL path for ``filename``, a model image.

    These are files in the model's base directory.
    """
    return request.static_url('webgnome:static/%s/%s/%s' % (
        request.registry.settings['model_images_url_path'],
        model.id,
        filename))


def get_runtime():
    """
    Return the current time as a string to be used as part of the file path
    for all images generated during a model run.
    """
    return time.strftime("%Y-%m-%d-%H-%M-%S")


def delete_keys_from_dict(target_dict, keys):
    """
    Recursively delete keys in `keys` from ``target_dict``.
    """
    def walk(value):
        if isinstance(value, dict):
            delete_keys_from_dict(value, keys)
        elif hasattr(value, '__iter__'):
            for val in value:
                walk(val)

    for k in keys:
        try:
            del target_dict[k]
        except KeyError:
            pass
    for value in target_dict.values():
        walk(value)

    return target_dict


def mkdir_p(path):
    """
    Make direction at ``path`` by first creating parent directories, unless they
    already exist. Similar to `mkdir -p` functionality in Unix/Linux.

    http://stackoverflow.com/questions/600268/mkdir-p-functionality-in-python
    """
    try:
        os.makedirs(path)
    except OSError as exc: # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def monkey_patch_colander():
    import colander

    # Recover boolean values which were coerced into strings.
    serialize_boolean = getattr(colander.Boolean, 'serialize')

    def patched_boolean_serialization(*args, **kwds):
        result = serialize_boolean(*args, **kwds)
        if result is not colander.null:
            result = result == 'true'
        return result

    setattr(colander.Boolean, 'serialize', patched_boolean_serialization)

    # Recover float values which were coerced into strings.
    serialize_float = getattr(colander.Float, 'serialize')

    def patched_float_serialization(*args, **kwds):
        result = serialize_float(*args, **kwds)
        if result is not colander.null:
            result = float(result)
        return result

    setattr(colander.Float, 'serialize', patched_float_serialization)

    # Recover integer values which were coerced into strings.
    serialize_int = getattr(colander.Int, 'serialize')

    def patched_int_serialization(*args, **kwds):
        result = serialize_int(*args, **kwds)
        if result is not colander.null:
            result = int(result)
        return result

    setattr(colander.Int, 'serialize', patched_int_serialization)

    # Remove optional mapping keys which were associated with 'colander.null'.
    serialize_mapping = getattr(colander.MappingSchema, 'serialize')

    def patched_mapping_serialization(*args, **kwds):
        result = serialize_mapping(*args, **kwds)
        if result is not colander.null:
            result = {k: v for k, v in result.iteritems() if
                      v is not colander.null}
        return result

    setattr(colander.MappingSchema, 'serialize', patched_mapping_serialization)


velocity_unit_values = list(chain.from_iterable(
    [item[1] for item in ConvertDataUnits['Velocity'].values()]))


class CleanDirectoryCommand(object):
    def __init__(self, directory, description):
        self.directory = directory
        self.description = description

    def __call__(self):
        parser = argparse.ArgumentParser(description=self.description)
        parser.add_argument('--simulate', action='store_true', dest='simulate')

        args = parser.parse_args()

        if not os.path.exists(self.directory):
            print >> sys.stderr, \
                'Directory does not exist: %s' % self.directory

        files = os.listdir(self.directory)
        files.sort()

        if not args.simulate:
            shutil.rmtree(self.directory)
            os.mkdir(self.directory)

        print 'Files and directories deleted:\n%s' % '\n'.join(files)
        print 'Total (top-level): %s' % len(files)


def dirnames(path):
    """
    Return a list of the names of all directories found in ``path``.
    """
    return [name for name in os.listdir(path) if
            os.path.isdir(os.path.join(path, name))]


def get_location_files(location_file_dir, ignored_files=None):
    """
    Return a list of valid location file names -- the names of directories found
    in the path ``location_files_dir`` which contain a `config.json` file.

    Ignores the `templates` directory.
    """
    listing = []
    location_files = []
    ignored_files = ignored_files or []

    try:
        listing = dirnames(location_file_dir)
    except OSError as e:
        logger.error('Could not access location file at path: %s. Error: %s' % (
            location_file_dir, e))

    for ignored in ignored_files:
        listing.pop(listing.index(ignored))

    for location_file in listing:
        config = os.path.join(location_file_dir, location_file, 'config.json')

        if not os.path.exists(config):
            logger.error(
                'Location file does not contain a conf.json file: '
                '%s. Path: %s' % (location_file, config))
            continue

        location_files.append(location_file)

    return location_files


class LocationFileExists(OSError):
    pass


def create_location_file(path, **data):
    """
    Create a Location File directory at ``path`` and populate it with a skeleton
    of required files.
    """
    if os.path.exists(path):
        raise LocationFileExists

    json_config = json.dumps(data)

    mkdir_p(path)
    mkdir_p(os.path.join(path, 'data'))

    # Make the directory a Python package.
    with open(os.path.join(path, '__init__.py'), 'wb') as f:
        f.write('')

    with open(os.path.join(path, 'config.json'), 'wb') as f:
        f.write(json_config)

    return json_config


def safe_join(directory, filename):
    """
    Safely join `directory` and `filename`.  If this cannot be done,
    this function returns ``None``.

    :param directory: the base directory.
    :param filename: the untrusted filename relative to that directory.

    :copyright: (c) 2011 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
    """
    _os_alt_seps = list(sep for sep in [os.path.sep, os.path.altsep]
                    if sep not in (None, '/'))
    filename = posixpath.normpath(filename)
    for sep in _os_alt_seps:
        if sep in filename:
            return None
    if os.path.isabs(filename) or filename.startswith('../'):
        return None
    return os.path.join(directory, filename)
