# Paste into the Ignition 8.3 Designer Script Console. Read-only, no file export.
import json
import traceback
from java.lang import Throwable


def probe_tag_serialization():
    folder = '[default]Line_1/Cupper/Ignition/'
    properties = ('dataType', 'valueSource', 'defaultValue', 'historyEnabled', 'historyProvider')

    def run(report, stage, operation):
        try:
            result = operation()
            report[stage] = {'ok': True, 'type': type(result).__name__}
            return result
        except (Exception, Throwable):
            report[stage] = {'ok': False, 'trace': traceback.format_exc(limit=8)}
            return None

    paths = [folder + name for name in ('Outfeed_Counter', 'Status', 'Speed', 'Production')]
    for path in paths:
        report = {'path': path}
        exported = run(report, 'native_export', lambda: system.tag.exportTags(
            tagPaths=[path], recursive=False, exportType='json'))
        if exported is not None:
            config = run(report, 'parse_export', lambda: json.loads(exported))
            if config is not None:
                report['config'] = config
                envelope = {'ok': True, 'data': {'path': path, 'config': config}}
                run(report, 'encode_webdev_envelope', lambda: system.util.jsonEncode(envelope))
        property_paths = [path] + [path + '.' + property_name for property_name in properties]
        values = run(report, 'readBlocking', lambda: system.tag.readBlocking(property_paths))
        if values is not None:
            report['values'] = {}
            for key, qv in zip(('value',) + properties, values):
                value = qv.value
                if value is not None and not isinstance(value, (int, long, float, bool, basestring)):
                    value = unicode(value)
                report['values'][key] = {'value': value, 'quality': str(qv.quality)}
        print(json.dumps(report, indent=2, sort_keys=True))

    # Print the native results before exercising the legacy Java serializer.
    for path in paths:
        report = {'path': path}
        raw = run(report, 'getConfiguration', lambda: system.tag.getConfiguration(path, False))
        if raw is not None and len(raw) == 1:
            encoded = run(report, 'legacy_jsonEncode', lambda: system.util.jsonEncode(raw[0]))
            if encoded is not None:
                run(report, 'legacy_jsonDecode', lambda: system.util.jsonDecode(encoded))
        print(json.dumps(report, indent=2, sort_keys=True))


probe_tag_serialization()
