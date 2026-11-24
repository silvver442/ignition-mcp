# Jython fragment for the dedicated Cupper source migration probe.


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
