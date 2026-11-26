# mcp_bridge_lib — project script library holding ALL bridge logic (Jython 2.7).
# The WebDev python resource "api" is a one-liner delegating here:
#   def doPost(request, session): return mcp_bridge_lib.handle(request)
# Mounted at: POST /system/webdev/mcp-bridge/api/<endpoint>
#
# Request body is a signed envelope produced by ignition-mcp's BridgeClient:
#   {"ts": "<unix seconds>", "sig": hex hmac-sha256(secret, ts + "." + payload),
#    "payload": "<json string>"}
#
# SECURITY-CRITICAL FILE — hand-written and reviewed. Handler functions below the
# dispatch table are generated against the handler contract in tasks/context/webdev.md.

import hashlib
import hmac
import os
import codecs
import errno
import stat

import math
import re
import threading
import json
BRIDGE_VERSION = '0.1.13'

# Set at install time (bridge_install rewrites this constant, or edit manually).
BRIDGE_SECRET = '__BRIDGE_SECRET__'

MAX_SKEW_SECONDS = 300
TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))
TAG_CONFIG_PATHS = frozenset(('[default]Line_1/BM1/Ignition/Status', '[default]Line_1/BM1/Ignition/Speed', '[default]Line_1/BM1/Ignition/Outfeed_Counter', '[default]Line_1/BM1/Ignition/Production'))
TAG_CONFIG_LOCK = threading.RLock()
TAG_HISTORY_PROPERTIES = frozenset((
    'historyEnabled', 'historyProvider', 'sampleMode', 'historySampleRate',
    'historySampleRateUnits', 'historyTagGroup', 'historicalDeadbandStyle',
    'historicalDeadbandMode', 'historicalDeadband',
))
TAG_HISTORY_CHOICES = {
    'sampleMode': ('OnChange', 'Periodic', 'TagGroup'),
    'historySampleRateUnits': ('MS', 'SEC', 'MIN', 'HOUR', 'DAY', 'WEEK', 'MONTH', 'YEAR'),
    'historicalDeadbandStyle': ('Auto', 'Analog_Compressed', 'Discrete'),
    'historicalDeadbandMode': ('Absolute', 'Percent', 'Off'),
}
TAG_READ_PROPERTIES = ('dataType', 'valueSource', 'defaultValue', 'opcServer', 'opcItemPath',
                       'sourceTagPath', 'tagGroup') + tuple(sorted(TAG_HISTORY_PROPERTIES))

LOGGER = system.util.getLogger('mcp-bridge')

PROJECTS_ROOT = os.path.abspath(
    os.path.join(system.util.getProperty('user.dir'), 'data', 'projects'))


# --------------------------------------------------------------------- helpers

def _err(message, remediation=''):
    return {'__error__': True, 'error': str(message), 'remediation': remediation}


def _is_err(result):
    return isinstance(result, dict) and result.get('__error__') is True


def _fmt_date(java_date):
    if java_date is None:
        return None
    try:
        return system.date.format(java_date, 'yyyy-MM-dd HH:mm:ss')
    except Exception:
        return str(java_date)


def _parse_when(s):
    """ISO 'yyyy-MM-ddTHH:mm:ss', relative '-8h'/'-30m'/'-2d', or '' (= now)."""
    if not s:
        return system.date.now()
    s = s.strip()
    if s.startswith('-') and len(s) >= 3 and s[-1] in ('m', 'h', 'd'):
        try:
            amount = int(s[1:-1])
        except ValueError:
            raise ValueError('bad relative time: %s' % s)
        unit = s[-1]
        now = system.date.now()
        if unit == 'm':
            return system.date.addMinutes(now, -amount)
        if unit == 'h':
            return system.date.addHours(now, -amount)
        return system.date.addDays(now, -amount)
    iso = s.replace('T', ' ')
    if len(iso) == 10:
        iso = iso + ' 00:00:00'
    return system.date.parse(iso, 'yyyy-MM-dd HH:mm:ss')


def _json_safe(value):
    if value is None or isinstance(value, (int, long, float, bool, str, unicode)):
        return value
    try:
        return _fmt_date(value)
    except Exception:
        return str(value)


def _ds_to_table(dataset):
    columns = [str(dataset.getColumnName(c)) for c in range(dataset.getColumnCount())]
    rows = []
    for r in range(dataset.getRowCount()):
        rows.append([_json_safe(dataset.getValueAt(r, c)) for c in range(len(columns))])
    return {'columns': columns, 'rows': rows}


def _safe_path(rel):
    """Resolve a path relative to PROJECTS_ROOT; refuse traversal outside it."""
    if not rel or rel.startswith('/') or rel.startswith('\\'):
        raise ValueError('path must be relative: %s' % rel)
    resolved = os.path.abspath(os.path.join(PROJECTS_ROOT, rel))
    if not (resolved == PROJECTS_ROOT or resolved.startswith(PROJECTS_ROOT + os.sep)):
        raise ValueError('path escapes data/projects: %s' % rel)
    return resolved


def _compare_digest(a, b):
    try:
        return hmac.compare_digest(a, b)
    except AttributeError:
        if len(a) != len(b):
            return False
        result = 0
        for x, y in zip(a, b):
            result |= ord(x) ^ ord(y)
        return result == 0


def _verify(envelope, cupper=False):
    """Returns (payload_dict, None) on success, (None, http_response_dict) on failure."""
    def reject():
        return {'json': {'ok': False, 'code': 'auth', 'error': 'authentication failed'}}

    if BRIDGE_SECRET == '__BRIDGE_SECRET__':
        return None, {'json': {'ok': False, 'code': 'auth',
                               'error': 'bridge secret not configured',
                               'remediation': 'Set BRIDGE_SECRET in the mcp-bridge api resource.'}}
    if not isinstance(envelope, dict):
        return None, reject()
    ts = envelope.get('ts')
    sig = envelope.get('sig')
    payload_str = envelope.get('payload')
    if not ts or not sig or payload_str is None:
        return None, reject()
    try:
        skew = abs(system.date.toMillis(system.date.now()) / 1000 - long(ts))
    except Exception:
        return None, reject()
    if skew > MAX_SKEW_SECONDS:
        return None, reject()
    if isinstance(payload_str, unicode):
        payload_bytes = payload_str.encode('utf-8')
    else:
        payload_bytes = str(payload_str)
    message = (str(ts) + '.').encode('utf-8') + payload_bytes
    secret_bytes = BRIDGE_SECRET.encode('utf-8') if isinstance(BRIDGE_SECRET, unicode) else BRIDGE_SECRET
    expected = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()
    if not _compare_digest(str(expected), str(sig)):
        return None, reject()
    try:
        payload = json.loads(payload_str) if cupper else system.util.jsonDecode(payload_str)
        if cupper and not isinstance(payload, dict):
            raise ValueError('Expected a JSON object')
    except Exception:
        return None, {'json': {'ok': False, 'error': 'payload is not valid JSON'}}
    return payload or {}, None


# ------------------------------------------------------------------- handlers

def handle_ping(payload):
    return {'version': BRIDGE_VERSION,
            'capabilities': sorted(HANDLERS.keys())}


# Generated handlers are inserted below this line (see tasks/010-013). ----------

def handle_tags_browse(payload):
    path = payload.get('path') or ''
    provider = payload.get('provider') or 'default'
    depth = int(payload.get('depth') or 1)
    root = '[%s]%s' % (provider, path)

    def _browse(browse_path, level):
        results = system.tag.browse(browse_path).getResults()
        nodes = []
        for r in results:
            node = {
                'fullPath': str(r['fullPath']),
                'name': str(r['name']),
                'tagType': str(r['tagType']),
                'dataType': str(r.get('dataType')) if r.get('dataType') else None,
                'hasChildren': bool(r['hasChildren'])
            }
            if r['hasChildren'] and level < depth:
                node['children'] = _browse(str(r['fullPath']), level + 1)
            nodes.append(node)
        return nodes

    return {'results': _browse(root, 1)}

def handle_tags_read(payload):
    paths = payload.get('paths') or []
    if not paths:
        return _err('paths is required')
    values = system.tag.readBlocking(paths)
    tags = []
    for i in range(len(paths)):
        tags.append({
            'path': paths[i],
            'value': _json_safe(values[i].value),
            'quality': str(values[i].quality),
            'timestamp': _fmt_date(values[i].timestamp)
        })
    return {'tags': tags}

def _scoped_tag_path(path):
    if not isinstance(path, basestring):
        raise ValueError('path is required')
    parent, _, name = path.rpartition('/')
    if ((parent not in TAG_CONFIG_ROOTS and path not in TAG_CONFIG_PATHS)
            or not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,254}\Z', name)
            or name.lower() in ('plc', '_types_')):
        raise ValueError('Expected a direct tag under an approved Ignition folder')
    folder = '[default]'
    for segment in parent[len(folder):].split('/'):
        node = _scoped_find_child(folder, segment)
        if node != 'Folder':
            raise ValueError('The approved path must consist of existing ordinary folders')
        delimiter = '' if folder == '[default]' else '/'
        folder += delimiter + segment
    return parent, name


def _scoped_find_child(parent, name):
    matches = [node for node in system.tag.browse(parent).getResults()
               if unicode(node['name']).lower() == name.lower()]
    if not matches:
        return None
    if len(matches) != 1 or unicode(matches[0]['name']) != name:
        raise ValueError('Tag or folder name differs in case or is ambiguous')
    return str(matches[0]['tagType'])


def _scoped_export_config(path, name):
    # Ignition's own exporter handles tag property types before Jython sees them.
    exported = json.loads(system.tag.exportTags(tagPaths=[path], recursive=False, exportType='json'))
    if not isinstance(exported, dict):
        raise ValueError('Expected a JSON tag export object')
    if 'tags' in exported:
        tags = exported['tags']
        if not isinstance(tags, list) or len(tags) != 1:
            raise ValueError('Expected exactly one tag in the JSON export')
        config = tags[0]
    else:
        config = exported
    if (not isinstance(config, dict) or config.get('name') != name
            or config.get('tagType', 'AtomicTag') != 'AtomicTag' or 'tags' in config):
        raise ValueError('Unexpected tag configuration in the JSON export')
    return config


def _scoped_tag_snapshot(path, parent, name):
    kind = _scoped_find_child(parent, name)
    if kind is None:
        return {'path': path, 'config': None, 'effective': {}, 'value': None, 'valueGood': False}
    if kind != 'AtomicTag':
        raise ValueError('Only standalone atomic tags are supported')
    config = _scoped_export_config(path, name)
    values = system.tag.readBlocking([path] + [path + '.' + prop for prop in TAG_READ_PROPERTIES])
    effective = {}
    for index, prop in enumerate(TAG_READ_PROPERTIES, 1):
        datum = values[index]
        if datum.quality.isGood():
            effective[prop] = _json_safe(datum.value)
    return {'path': path, 'config': config, 'effective': effective,
            'value': _json_safe(values[0].value), 'valueGood': values[0].quality.isGood()}


def handle_tags_scoped_state(payload):
    path = payload.get('path')
    with TAG_CONFIG_LOCK:
        parent, name = _scoped_tag_path(path)
        return _scoped_tag_snapshot(path, parent, name)


def _scoped_result(results):
    failure = 'Tag operation did not return one Good result'
    if results is None:
        raise ValueError(failure + ' (result=null)')
    try:
        count = len(results)
    except TypeError:
        raise ValueError(failure + ' (result is not a list)')

    codes = []
    for index in range(min(count, 8)):
        try:
            code = results[index]
            if code is None:
                codes.append('null')
                continue
            if count == 1 and code.isGood():
                return
            # The SDK name and number exclude the arbitrary diagnostic message.
            codes.append('%s (%d)' % (code.getName(), code.getCode()))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            codes.append('invalid QualityCode')
    if count > len(codes):
        codes.append('%d more' % (count - len(codes)))
    raise ValueError('%s (count=%d, quality=[%s])' % (failure, count, ', '.join(codes)))


def _valid_history_patch(patch):
    if not isinstance(patch, dict) or not patch or not set(patch).issubset(TAG_HISTORY_PROPERTIES):
        return False
    for key, value in patch.items():
        if key in TAG_HISTORY_CHOICES:
            valid = value in TAG_HISTORY_CHOICES[key]
        elif key == 'historyEnabled':
            valid = isinstance(value, bool)
        elif key == 'historySampleRate':
            valid = isinstance(value, (int, long)) and not isinstance(value, bool) and 0 < value < 2 ** 31
        elif key == 'historicalDeadband':
            valid = (isinstance(value, (int, long, float)) and not isinstance(value, bool)
                     and 0 <= value and not math.isinf(value))
        else:
            valid = isinstance(value, basestring) and 0 < len(value.strip()) <= 255
        if not valid:
            return False
    return True


def handle_tags_scoped_configure(payload):
    path = payload.get('path')
    action = payload.get('action')
    with TAG_CONFIG_LOCK:
        parent, name = _scoped_tag_path(path)
        before = _scoped_tag_snapshot(path, parent, name)
        if 'expectedConfig' not in payload or before['config'] != payload['expectedConfig']:
            raise ValueError('Tag configuration changed before the write')
        if action == 'create':
            data_type = payload.get('dataType')
            value = payload.get('value')
            bits = {'Int4': 32, 'Int8': 64}.get(data_type)
            if (before['config'] is not None or bits is None or isinstance(value, bool)
                    or not isinstance(value, (int, long)) or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)):
                raise ValueError('Expected an absent tag and a valid Memory integer')
            patch = {'tagType': 'AtomicTag', 'dataType': data_type, 'valueSource': 'memory',
                     'historyEnabled': False, 'value': value}
            policy = 'a'
        elif action == 'history':
            patch = payload.get('history')
            if before['config'] is None or not _valid_history_patch(patch):
                raise ValueError('Expected an existing tag and a scalar history patch')
            policy = 'm'
        else:
            raise ValueError('Unsupported tag configuration operation')
        _scoped_result(system.tag.configure(parent, [dict(patch, name=name)], policy))
        return _scoped_tag_snapshot(path, parent, name)


def handle_tags_scoped_restore(payload):
    path = payload.get('path')
    with TAG_CONFIG_LOCK:
        parent, name = _scoped_tag_path(path)
        current = _scoped_tag_snapshot(path, parent, name)
        if ('beforeConfig' not in payload or current['config'] is None
                or current['config'] != payload.get('expectedConfig')):
            raise ValueError('Tag configuration changed before restore')
        prior = payload['beforeConfig']
        source = str(current['effective'].get('valueSource', current['config'].get('valueSource'))).lower()
        if prior is None or source == 'memory':
            if (source != 'memory' or not current['valueGood']
                    or 'expectedValue' not in payload or current['value'] != payload['expectedValue']):
                raise ValueError('Memory tag value changed before restore')
        if prior is None:
            data_type = current['effective'].get('dataType', current['config'].get('dataType'))
            history = current['effective'].get('historyEnabled', current['config'].get('historyEnabled'))
            if data_type not in ('Int4', 'Int8') or history is not False:
                raise ValueError('Only verified Memory integer creations with history disabled can be undone')
            _scoped_result(system.tag.deleteTags([path]))
        else:
            if (not isinstance(prior, dict) or prior.get('name') != name
                    or prior.get('tagType', 'AtomicTag') != 'AtomicTag' or 'tags' in prior):
                raise ValueError('Restore requires the original atomic tag configuration')
            _scoped_result(system.tag.configure(parent, [prior], 'o'))
        return _scoped_tag_snapshot(path, parent, name)


def handle_tags_write(payload):
    writes = payload.get('writes') or []
    if not writes:
        return _err('writes is required')
    paths = [w['path'] for w in writes]
    values = [w['value'] for w in writes]
    results = system.tag.writeBlocking(paths, values)
    tags = []
    for i in range(len(paths)):
        tags.append({
            'path': paths[i],
            'quality': str(results[i])
        })
    return {'results': tags}

def handle_tags_configure(payload):
    base_path = payload.get('basePath') or '[default]'
    tags = payload.get('tags') or []
    if not tags:
        return _err('tags is required')
    policy = payload.get('collisionPolicy') or 'a'
    results = system.tag.configure(base_path, tags, policy)
    return {'results': [str(qc) for qc in results]}


def handle_history_query(payload):
    paths = payload.get('paths') or []
    if not paths:
        return _err('paths is required')
    start = _parse_when(payload.get('start') or '-8h')
    end = _parse_when(payload.get('end') or '')
    return_size = int(payload.get('returnSize') or 300)
    aggregation = payload.get('aggregation') or 'Average'
    try:
        ds = system.tag.queryTagHistory(paths=paths, startDate=start, endDate=end,
          returnSize=return_size, aggregationMode=aggregation, returnFormat='Wide')
        return _ds_to_table(ds)
    except Exception as e:
        return _err('query failed: %s' % str(e))

def handle_history_providers(payload):
    providers = []
    try:
        from com.inductiveautomation.ignition.gateway import IgnitionGateway
        context = IgnitionGateway.get()
        mgr = context.getHistoryManager()
        for p in mgr.getStores():
            providers.append(str(p))
    except Exception as e:
        return _err('could not enumerate history providers: %s' % str(e))
    return {'providers': providers}

def handle_alarms_status(payload):
    states = payload.get('states') or ['ActiveUnacked', 'ActiveAcked', 'ClearUnacked', 'ClearAcked']
    source = payload.get('source') or '*'
    display = payload.get('displayPath') or '*'
    limit = int(payload.get('limit') or 100)
    events = system.alarm.queryStatus(state=states, source=['*%s*' % source if source != '*' else '*'],
      displaypath=['*%s*' % display if display != '*' else '*'])
    collected = []
    for e in events:
        collected.append({
            'id': str(e.getId()),
            'name': str(e.getName()),
            'source': str(e.getSource()),
            'displayPath': str(e.getDisplayPath()),
            'priority': str(e.getPriority()),
            'state': str(e.getState())
        })
        if len(collected) >= limit:
            break
    return {'alarms': collected, 'truncated': len(events) > limit}

def handle_alarms_journal(payload):
    start = _parse_when(payload.get('start') or '-24h')
    end = _parse_when(payload.get('end') or '')
    states = payload.get('states') or None
    source = payload.get('source') or '*'
    limit = int(payload.get('limit') or 200)
    kwargs = {'startDate': start, 'endDate': end}
    if states:
        kwargs['state'] = states
    if source != '*':
        kwargs['source'] = ['*%s*' % source]
    events = system.alarm.queryJournal(**kwargs)
    collected = []
    for e in events:
        event_time = _fmt_date(e.getLastEventTime()) if hasattr(e, 'getLastEventTime') else None
        collected.append({
            'id': str(e.getId()),
            'name': str(e.getName()),
            'source': str(e.getSource()),
            'displayPath': str(e.getDisplayPath()),
            'priority': str(e.getPriority()),
            'state': str(e.getState()),
            'eventTime': event_time
        })
        if len(collected) >= limit:
            break
    return {'events': collected, 'truncated': len(events) > limit}

def handle_alarms_ack(payload):
    ids = payload.get('eventIds') or []
    if not ids:
        return _err('eventIds is required')
    notes = payload.get('notes') or None
    try:
        system.alarm.acknowledge(ids, notes)
        return {'acknowledged': len(ids)}
    except Exception as e:
        return _err('acknowledge failed: %s' % str(e))


def handle_db_named_query(payload):
    project = payload.get('project')
    path = payload.get('path')
    if not project or not path:
        return _err('project and path are required')
    params = payload.get('params') or {}
    try:
        result = system.db.runNamedQuery(project, path, params)
        if hasattr(result, 'getColumnCount'):
            return _ds_to_table(result)
        else:
            return {'value': _json_safe(result)}
    except Exception as e:
        return _err('named query failed: %s' % str(e))


def handle_db_query(payload):
    sql = payload.get('sql')
    if not sql:
        return _err('sql is required')
    database = payload.get('database') or ''
    args = payload.get('args') or []
    mutating = bool(payload.get('mutating'))
    try:
        if mutating:
            count = system.db.runPrepUpdate(sql, args, database)
            return {'rowsAffected': count}
        else:
            ds = system.db.runPrepQuery(sql, args, database)
            table = _ds_to_table(ds)
            if len(table['rows']) > 5000:
                table['rows'] = table['rows'][:5000]
                table['truncated'] = True
            return table
    except Exception as e:
        return _err('query failed: %s' % str(e))


def handle_files_list(payload):
    rel = payload.get('path') or ''
    recursive = bool(payload.get('recursive'))
    root = _safe_path(rel)
    if not os.path.isdir(root):
        return {'entries': []}
    entries = []
    if recursive:
        walker = os.walk(root)
    else:
        names = os.listdir(root)
        walker = [(root, [n for n in names if os.path.isdir(os.path.join(root, n))],
                   [n for n in names if not os.path.isdir(os.path.join(root, n))])]
    for dirpath, dirnames, filenames in walker:
        if '.resources' in dirnames:
            dirnames.remove('.resources')
        for name in dirnames + filenames:
            full_path = os.path.join(dirpath, name)
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            entries.append({
                'path': full_path[len(PROJECTS_ROOT):].replace(os.sep, '/').lstrip('/'),
                'isDir': is_dir,
                'size': size
            })
    return {'entries': entries}


def handle_files_read(payload):
    rel = payload.get('path')
    if not rel:
        return _err('path is required')
    full = _safe_path(rel)
    try:
        mode = os.stat(full).st_mode
    except OSError as e:
        if e.errno == errno.ENOENT:
            result = _err('file not found: %s' % rel)
            result['code'] = 'not_found'
            return result
        return _err('read failed: %s' % str(e))
    if not stat.S_ISREG(mode):
        return _err('not a regular file: %s' % rel)
    try:
        if payload.get('format') == 'sha256':
            digest = hashlib.sha256()
            with open(full, 'rb') as f:
                for block in iter(lambda: f.read(65536), b''):
                    digest.update(block)
            return {'sha256': digest.hexdigest()}
        with codecs.open(full, 'r', 'utf-8') as f:
            content = f.read()
        return {'content': content}
    except Exception as e:
        return _err('read failed: %s' % str(e))


def handle_files_write(payload):
    rel = payload.get('path')
    content = payload.get('content')
    if not rel or content is None:
        return _err('path and content are required')
    full = _safe_path(rel)
    try:
        parent = os.path.dirname(full)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with codecs.open(full, 'w', 'utf-8') as f:
            f.write(content)
        return {'written': rel, 'bytes': len(content)}
    except Exception as e:
        return _err('write failed: %s' % str(e))


def handle_files_delete(payload):
    rel = payload.get('path')
    if not rel:
        return _err('path is required')
    recursive = bool(payload.get('recursive'))
    full = _safe_path(rel)
    try:
        if os.path.isfile(full):
            os.remove(full)
            return {'deleted': rel}
        elif os.path.isdir(full):
            if recursive:
                import shutil
                shutil.rmtree(full)
            else:
                os.rmdir(full)
            return {'deleted': rel}
        else:
            return _err('not found: %s' % rel)
    except Exception as e:
        return _err('delete failed: %s' % str(e))


def handle_trial_reset(payload):
    try:
        from com.inductiveautomation.ignition.gateway import IgnitionGateway
        context = IgnitionGateway.get()
        lm = context.getLicenseManager()
        if hasattr(lm, 'resetTrial'):
            lm.resetTrial()
        else:
            for method_name in dir(lm):
                if 'resetTrial' in method_name or 'trialReset' in method_name:
                    getattr(lm, method_name)()
                    break
            else:
                return _err('no trial reset method on LicenseManager', 'Reset the trial manually on the gateway homepage.')
        return {'reset': True}
    except Exception as e:
        return _err('trial reset failed: %s' % str(e), 'Reset the trial manually on the gateway homepage.')


# ------------------------------------------------------------------- dispatch

# Jython fragment inserted into the Bridge library by apply_upgrade_013.py.

def _cupper_tag_name(path):
    root = '[default]Line_1/Cupper/Ignition/'
    if path not in (root + 'Speed', root + 'Production'):
        raise ValueError('Migration is limited to Cupper Speed and Production')
    return path.rsplit('/', 1)[1]


def _cupper_integer(value):
    return isinstance(value, (int, long)) and not isinstance(value, bool)


def _cupper_history_proof(start=None, end=None, name='Production'):
    if name not in ('Production', 'Speed'):
        raise ValueError('History proof is limited to Cupper Production and Speed')
    window = 1800000 if name == 'Speed' else 7 * 86400000
    cutoff = system.date.now().getTime() - 300000
    if start is None and end is None:
        end = cutoff
        start = end - window
    if (not _cupper_integer(start) or not _cupper_integer(end)
            or not 0 <= start < end <= cutoff or end - start > window):
        limit = '30 minutes' if name == 'Speed' else 'seven days'
        raise ValueError('Expected a closed historical window of at most ' + limit)
    path = 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/' + name
    options = {'returnSize': -1} if name == 'Speed' else {}
    dataset = system.historian.queryRawPoints(paths=[path], startTime=system.date.fromMillis(start),
                                             endTime=system.date.fromMillis(end), returnFormat='TALL', **options)
    if dataset.getColumnCount() != 4:
        raise ValueError('Expected a timestamp, value and quality from SQL Historian')
    rows = []
    numeric_samples = 0
    for index in range(dataset.getRowCount()):
        timestamp = dataset.getValueAt(index, 3).getTime()
        if start <= timestamp < end:
            value = dataset.getValueAt(index, 1)
            if value is not None and not _cupper_integer(value):
                raise ValueError('Expected integer samples from SQL Historian')
            if value is not None:
                numeric_samples += 1
            quality = dataset.getValueAt(index, 2)
            rows.append(json.dumps([timestamp, value, str(quality)], separators=(',', ':')))
    if rows and not numeric_samples:
        raise ValueError('SQL Historian returned no numeric %s samples for the window' % name)
    digest = hashlib.sha256('\n'.join(sorted(rows)).encode('utf-8')).hexdigest()
    return {'start': start, 'end': end, 'count': len(rows), 'sha256': digest}


def handle_tags_scoped_cupper_history(payload):
    path = payload.get('path')
    name = _cupper_tag_name(path)
    if name == 'Production' and set(payload) == {'path', 'start_ms', 'end_ms'}:
        return _cupper_raw_history(path, payload['start_ms'], payload['end_ms'])
    if set(payload) not in ({'path'}, {'path', 'start', 'end'}) or (
            'start' in payload and (payload['start'] is None or payload['end'] is None)):
        raise ValueError('Expected both legacy history boundaries')
    return _cupper_history_proof(payload.get('start'), payload.get('end'), name)


def _cupper_same_config(first, second, fields):
    return (dict((k, v) for k, v in first.items() if k not in fields)
            == dict((k, v) for k, v in second.items() if k not in fields))


def _cupper_require_snapshot(snapshot, data_type, source):
    effective = snapshot['effective']
    if (snapshot['config'] is None or effective.get('dataType') != data_type
            or effective.get('valueSource') != source or effective.get('historyEnabled') is not True
            or effective.get('historyProvider') != 'Postgres_Historian'
            or not snapshot['valueGood'] or not _cupper_integer(snapshot['value'])):
        raise ValueError('Unexpected Cupper type, source, history or live value')


def handle_tags_scoped_cupper_migrate(payload):
    name, mode = _cupper_migration_request(payload)
    path = payload.get('path')
    restoring = mode == 'restore'
    with TAG_CONFIG_LOCK:
        parent, _ = _scoped_tag_path(path)
        before = _scoped_tag_snapshot(path, parent, name)
        config = before['config']
        if config != payload.get('expectedConfig'):
            raise ValueError('Tag configuration changed before migration')
        value = before['value']
        if name == 'Production':
            _cupper_require_snapshot(before, 'Int8' if restoring else 'Int4', 'memory')
            proof = payload.get('historyProof')
            if (not isinstance(proof, dict) or not proof.get('count')
                    or _cupper_history_proof(proof.get('start'), proof.get('end')) != proof):
                raise ValueError('Production raw history no longer matches its preflight')
            if value != payload.get('expectedValue') or not -(2 ** 31) <= value < 2 ** 31:
                raise ValueError('Production counter changed or is outside the Int4 range')
            # Read again after the historian query and as close to configure as possible.
            latest = _scoped_tag_snapshot(path, parent, name)
            if latest != before:
                raise ValueError('Production changed during historian verification')
            patch = {'name': name, 'dataType': 'Int4' if restoring else 'Int8', 'value': value}
            _scoped_result(system.tag.configure(parent, [patch], 'm'))
        elif restoring:
            _cupper_require_snapshot(before, 'Int4', 'memory')
            prior = payload.get('beforeConfig')
            if (value != payload.get('expectedValue') or not isinstance(prior, dict)
                    or prior.get('valueSource') != 'expr' or prior.get('dataType', 'Int4') != 'Int4'
                    or not isinstance(prior.get('expression'), basestring) or not prior['expression']
                    or not _cupper_same_config(config, prior, ('valueSource', 'expression', 'value', 'defaultValue'))):
                raise ValueError('Speed has advanced or its original expression cannot be restored safely')
            _scoped_result(system.tag.configure(parent, [prior], 'o'))
        else:
            _cupper_require_snapshot(before, 'Int4', 'expr')
            proof = payload.get('historyProof')
            if (not isinstance(proof, dict) or not proof.get('count')
                    or _cupper_history_proof(proof.get('start'), proof.get('end'), name) != proof):
                raise ValueError('Speed raw history no longer matches its preflight')
            latest = _scoped_tag_snapshot(path, parent, name)
            _cupper_require_snapshot(latest, 'Int4', 'expr')
            effective = latest['effective']
            if (latest['config'] != config or effective != before['effective']
                    or effective.get('sampleMode') != 'Periodic' or effective.get('historySampleRate') != 10
                    or effective.get('historySampleRateUnits') != 'SEC'):
                raise ValueError('Speed configuration or 10-second history settings changed before migration')
            value = latest['value']
            patch = {'name': name, 'valueSource': 'memory', 'defaultValue': value, 'value': value}
            _scoped_result(system.tag.configure(parent, [patch], 'm'))
        return {'valueBefore': value}


# Jython fragment appended to the Bridge 0.1.3 library by apply_upgrade_014.py.

def _cupper_migration_request(payload):
    name = _cupper_tag_name(payload.get('path'))
    mode = payload.get('mode')
    if mode not in ('prepare', 'restore'):
        raise ValueError('Expected Cupper operation prepare or restore')
    if payload.get('exclusiveAccess') is not True:
        raise ValueError('Explicit exclusiveAccess boolean true is required')
    return name, mode


def handle_tags_scoped_cupper_validate(payload):
    name, mode = _cupper_migration_request(payload)
    path = payload['path']
    _scoped_tag_path(path)
    return {'valid': True, 'path': path, 'name': name, 'mode': mode, 'exclusiveAccess': True}


def _cupper_raw_history(path, start_ms, end_ms):
    if (not _cupper_integer(start_ms) or not _cupper_integer(end_ms)
            or not 0 <= start_ms < end_ms <= system.date.now().getTime()
            or end_ms - start_ms > 7 * 86400000):
        raise ValueError('Expected explicit start_ms and end_ms for a closed window of at most seven days')
    history_path = ('histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:'
                    '/tag:Line_1/Cupper/Ignition/Production')
    dataset = system.historian.queryRawPoints(
        paths=[history_path], startTime=system.date.fromMillis(start_ms),
        endTime=system.date.fromMillis(end_ms), returnFormat='TALL', returnSize=-1,
    )
    if dataset.getColumnCount() != 4:
        raise ValueError('Expected path, value, quality and timestamp in the TALL historian dataset')
    rows = []
    for index in range(dataset.getRowCount()):
        timestamp_ms = dataset.getValueAt(index, 3).getTime()
        if start_ms <= timestamp_ms <= end_ms:
            value = dataset.getValueAt(index, 1)
            if value is not None and not _cupper_integer(value):
                raise ValueError('Expected integer samples from SQL Historian')
            quality = dataset.getValueAt(index, 2)
            sample = {'timestamp_ms': timestamp_ms, 'value': value, 'path': path,
                      'quality': None if quality is None else unicode(quality)}
            encoded = json.dumps(sample, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
            rows.append((timestamp_ms, encoded, sample))
    rows.sort(key=lambda row: row[:2])
    digest = hashlib.sha256('\n'.join(row[1] for row in rows).encode('ascii')).hexdigest()
    return {'path': path, 'start_ms': start_ms, 'end_ms': end_ms, 'count': len(rows),
            'sha256': digest, 'samples': [row[2] for row in rows]}


# Additive Jython fragment for the dedicated Cupper source migration probe.


def _source_test_expression(snapshot):
    config = snapshot['config']
    effective = snapshot['effective']
    value = snapshot['value']
    return (config is not None and config.get('expression') == 'getsecond(now())'
            and effective.get('valueSource', config.get('valueSource')) == 'expr'
            and effective.get('dataType', config.get('dataType')) == 'Int4'
            and effective.get('historyEnabled', config.get('historyEnabled')) is False
            and snapshot['valueGood'] and isinstance(value, (int, long))
            and not isinstance(value, bool) and 0 <= value < 60)


def _source_test_quality(results, value):
    if results is None:
        return {'count': None, 'qualities': [], 'valueBefore': value}
    count = len(results)
    qualities = []
    for index in range(count):
        quality = results[index]
        qualities.append(None if quality is None else {
            'name': unicode(quality.getName()), 'code': int(quality.getCode()), 'good': bool(quality.isGood()),
        })
    return {'count': count, 'qualities': qualities, 'valueBefore': value}


def _source_test_create_result(results):
    result = _source_test_quality(results, None)
    if result['count'] != 1 or result['qualities'][0] is None or result['qualities'][0]['good']:
        return result
    try:
        message = results[0].getDiagnosticMessage()
        if not message:
            return result
        message = unicode(message)[:1024].lower().strip()
    except Exception:
        return result
    if not message:
        return result

    # Return only a fixed identifier. Ignition messages can embed sensitive input.
    reasons = (
        ('name_collision', ('already exists', 'duplicate tag', 'name collision')),
        ('invalid_path', ('invalid path', 'invalid tag path', 'parent folder does not exist')),
        ('invalid_expression', ('invalid expression', 'expression syntax', 'could not parse expression')),
        ('invalid_data_type', ('invalid data type', 'unsupported data type', 'unknown data type')),
        ('invalid_tag_group', ('invalid tag group', 'unknown tag group', 'tag group does not exist')),
        ('invalid_json', ('invalid json', 'malformed json', 'could not parse json')),
        ('permission_denied', ('permission denied', 'access denied', 'not authorized')),
    )
    result['diagnostic'] = 'redacted'
    for reason, phrases in reasons:
        if any(phrase in message for phrase in phrases):
            result['diagnostic'] = reason
            break
    return result


def handle_tags_scoped_cupper_source_test(payload):
    path = '[default]Line_1/Cupper/Ignition/MCP_Test_SourceMigration'
    if (set(payload) != {'path', 'action', 'expectedConfig', 'exclusiveAccess'}
            or payload['path'] != path or payload['exclusiveAccess'] is not True
            or payload['action'] not in ('create', 'merge', 'overwrite', 'undo_create')):
        raise ValueError('An exact source test operation and exclusive access are required')
    action = payload['action']
    with TAG_CONFIG_LOCK:
        parent, name = _scoped_tag_path(path)
        before = _scoped_tag_snapshot(path, parent, name)
        config = before['config']
        if config != payload['expectedConfig']:
            raise ValueError('Test tag configuration changed before the write')
        if action == 'create':
            if config is not None:
                raise ValueError('Test tag already exists')
            definition = {'name': name, 'tagType': 'AtomicTag', 'dataType': 'Int4',
                          'valueSource': 'expr', 'expression': 'getsecond(now())', 'historyEnabled': False}
            return _source_test_create_result(system.tag.configure(parent, [definition], 'a'))
        if not _source_test_expression(before):
            raise ValueError('The probe requires the reviewed Expression test tag with good quality')
        if action == 'undo_create':
            _scoped_result(system.tag.deleteTags([path]))
            return {'deleted': True}
        value = before['value']
        # Use the exact definition and collision policy of the existing Speed migration.
        patch = {'name': name, 'valueSource': 'memory', 'defaultValue': value, 'value': value}
        if action == 'merge':
            return _source_test_quality(system.tag.configure(parent, [patch], 'm'), value)
        definition = dict(config)
        del definition['expression']
        definition.update(patch)
        return _source_test_quality(system.tag.configure(parent, [definition], 'o'), value)


# Map exact tag paths to qualified historian paths reviewed by the Gateway operator.
# SQL Historian uses the drv syntax, while Core Historian uses sys/prov.
RAW_HISTORY_PATHS = {
    '[default]Line_1/Cupper/Ignition/Speed': 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Speed',
    '[default]Line_1/BM1/Ignition/Speed': 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/BM1/Ignition/Speed',
    '[default]Line_1/BM1/Ignition/Production': 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/BM1/Ignition/Production',
}


def handle_history_raw(payload):
    if set(payload) != {'path', 'start_ms', 'end_ms'}:
        raise ValueError('Expected path, start_ms and end_ms')
    path = payload['path']
    if not isinstance(path, basestring) or path not in RAW_HISTORY_PATHS:
        raise ValueError('Tag path is not allowed for raw history')
    start = payload['start_ms']
    end = payload['end_ms']
    if (any(not isinstance(value, (int, long)) or isinstance(value, bool) for value in (start, end))
            or not 0 <= start <= end < system.date.now().getTime()
            or end - start > 7 * 86400000):
        raise ValueError('Expected an inclusive historical Unix millisecond window of at most seven days')

    # Keep the requested endpoints inside the query and discard any outside rows.
    dataset = system.historian.queryRawPoints(
        paths=[RAW_HISTORY_PATHS[path]], startTime=system.date.fromMillis(start - 1),
        endTime=system.date.fromMillis(end + 1), returnFormat='TALL', returnSize=-1,
        includeBounds=False,
    )
    if dataset.getColumnCount() != 4:
        raise ValueError('Expected path, value, quality and timestamp from the Historian')
    samples = []
    for index in range(dataset.getRowCount()):
        timestamp = dataset.getValueAt(index, 3).getTime()
        if not start <= timestamp <= end:
            continue
        value = dataset.getValueAt(index, 1)
        if (value is not None and not isinstance(value, (bool, int, long, float, basestring))
                or isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
            raise ValueError('Historian returned a value that cannot be represented in JSON')
        quality = dataset.getValueAt(index, 2)
        if quality is None:
            raise ValueError('Historian returned a sample without quality')
        if len(samples) == 10000:
            raise ValueError('Raw history exceeds 10000 samples. Request a shorter window.')
        samples.append({'path': path, 'timestamp_ms': timestamp,
                        'value': value, 'quality': unicode(quality)})
    return {'path': path, 'start_ms': start, 'end_ms': end, 'count': len(samples), 'samples': samples}


HANDLERS = {
    'history/raw': handle_history_raw,
    'tags/scoped-cupper-source-test': handle_tags_scoped_cupper_source_test,
    'tags/scoped-cupper-history': handle_tags_scoped_cupper_history,
    'tags/scoped-cupper-migrate': handle_tags_scoped_cupper_migrate,
    'ping': handle_ping,
    'tags/browse': handle_tags_browse,
    'tags/read': handle_tags_read,
    'tags/write': handle_tags_write,
    'tags/configure': handle_tags_configure,
    'tags/scoped-state': handle_tags_scoped_state,
    'tags/scoped-configure': handle_tags_scoped_configure,
    'tags/scoped-restore': handle_tags_scoped_restore,
    'history/query': handle_history_query,
    'history/providers': handle_history_providers,
    'alarms/status': handle_alarms_status,
    'alarms/journal': handle_alarms_journal,
    'alarms/ack': handle_alarms_ack,
    'db/named-query': handle_db_named_query,
    'db/query': handle_db_query,
    'files/list': handle_files_list,
    'files/read': handle_files_read,
    'files/write': handle_files_write,
    'files/delete': handle_files_delete,
    'trial/reset': handle_trial_reset,
    'tags/scoped-cupper-validate': handle_tags_scoped_cupper_validate,
}


def handle(request):
    endpoint = (request.get('remainingPath') or '').strip('/')
    handler = HANDLERS.get(endpoint)
    if handler is None:
        return {'json': {'ok': False, 'error': 'unknown endpoint: %s' % endpoint,
                         'remediation': 'Known endpoints: %s' % ', '.join(sorted(HANDLERS))}}

    payload, reject_response = _verify(request.get('data'), endpoint.startswith('tags/scoped-cupper-') or endpoint == 'history/raw')
    if reject_response is not None:
        return reject_response

    try:
        result = handler(payload)
    except ValueError as e:
        return {'json': {'ok': False, 'error': str(e)}}
    except Exception as e:
        LOGGER.warn('mcp-bridge %s failed: %s' % (endpoint, str(e)))
        return {'json': {'ok': False, 'error': str(e)}}

    if _is_err(result):
        error = {'ok': False, 'error': result['error'],
                 'remediation': result.get('remediation', '')}
        if 'code' in result:
            error['code'] = result['code']
        return {'json': error}
    return {'json': {'ok': True, 'data': result}}
