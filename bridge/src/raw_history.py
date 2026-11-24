# Map exact tag paths to qualified historian paths reviewed by the Gateway operator.
# SQL Historian uses the drv syntax, while Core Historian uses sys/prov.
RAW_HISTORY_PATHS = {
    '[default]Line_1/Cupper/Ignition/Speed':
        'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Speed',
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
