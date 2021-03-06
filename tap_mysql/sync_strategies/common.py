#!/usr/bin/env python3
# pylint: disable=missing-function-docstring,too-many-arguments,too-many-locals
import os
import copy
import datetime
import singer
import time
import json
import uuid

from pathlib import Path
from singer import metadata, utils, metrics

from tap_mysql.stream_utils import get_key_properties

LOGGER = singer.get_logger('tap_mysql')

HOME = str(Path.home())
DEFAULT_FAST_SYNC_PATH = os.path.join(HOME, '.pipelinewise/tap_mysql_fast_sync_tmp/', str(uuid.uuid4()))


def escape(string):
    if '`' in string:
        raise Exception(f"Can't escape identifier {string} because it contains a backtick")
    return '`' + string + '`'


def generate_tap_stream_id(table_schema, table_name):
    return table_schema + '-' + table_name


def get_stream_version(tap_stream_id, state):
    stream_version = singer.get_bookmark(state, tap_stream_id, 'version')

    if stream_version is None:
        stream_version = int(time.time() * 1000)

    return stream_version


def stream_is_selected(stream):
    md_map = metadata.to_map(stream.metadata)
    selected_md = metadata.get(md_map, (), 'selected')

    return selected_md


def property_is_selected(stream, property_name):
    md_map = metadata.to_map(stream.metadata)
    return singer.should_sync_field(
        metadata.get(md_map, ('properties', property_name), 'inclusion'),
        metadata.get(md_map, ('properties', property_name), 'selected'),
        True)


def get_is_view(catalog_entry):
    md_map = metadata.to_map(catalog_entry.metadata)

    return md_map.get((), {}).get('is-view')


def get_database_name(catalog_entry):
    md_map = metadata.to_map(catalog_entry.metadata)

    return md_map.get((), {}).get('database-name')


def generate_select_sql(catalog_entry, columns):
    database_name = get_database_name(catalog_entry)
    escaped_db = escape(database_name)
    escaped_table = escape(catalog_entry.table)
    escaped_columns = []

    for idx, col_name in enumerate(columns):
        # wrap the column name in "`"
        escaped_col = escape(col_name)

        # fetch the column type format from the json schema already built
        property_format = catalog_entry.schema.properties[col_name].format

        # if the column format is binary, fetch the values after removing any trailing
        # null bytes 0x00 and hexifying the column.
        if 'binary' == property_format:
            escaped_columns.append(
                f'hex(trim(trailing CHAR(0x00) from {escaped_col})) as {escaped_col}')
        elif 'spatial' == property_format:
            escaped_columns.append(
                f'ST_AsGeoJSON({escaped_col}) as {escaped_col}')
        else:
            escaped_columns.append(escaped_col)

    select_sql = f'SELECT {",".join(escaped_columns)} FROM {escaped_db}.{escaped_table}'

    # escape percent signs
    select_sql = select_sql.replace('%', '%%')
    return select_sql


def row_to_singer_record(catalog_entry, version, row, columns, time_extracted):
    row_to_persist = ()
    for idx, elem in enumerate(row):
        property_type = catalog_entry.schema.properties[columns[idx]].type
        property_format = catalog_entry.schema.properties[columns[idx]].format

        if isinstance(elem, datetime.datetime):
            row_to_persist += (elem.isoformat() + '+00:00',)

        elif isinstance(elem, datetime.date):
            row_to_persist += (elem.isoformat() + 'T00:00:00+00:00',)

        elif isinstance(elem, datetime.timedelta):
            if property_format == 'time':
                row_to_persist += (str(elem),) # this should convert time column into 'HH:MM:SS' formatted string
            else:
                epoch = datetime.datetime.utcfromtimestamp(0)
                timedelta_from_epoch = epoch + elem
                row_to_persist += (timedelta_from_epoch.isoformat() + '+00:00',)

        elif 'boolean' in property_type or property_type == 'boolean':
            if elem is None:
                boolean_representation = None
            elif elem == 0 or elem == b'\x00':
                boolean_representation = False
            else:
                boolean_representation = True
            row_to_persist += (boolean_representation,)

        else:
            row_to_persist += (elem,)
    rec = dict(zip(columns, row_to_persist))

    return singer.RecordMessage(
        stream=catalog_entry.stream,
        record=rec,
        version=version,
        time_extracted=time_extracted)


def whitelist_bookmark_keys(bookmark_key_set, tap_stream_id, state):
    for bookmark_key in [non_whitelisted_bookmark_key for
                         non_whitelisted_bookmark_key in state.get('bookmarks', {}).get(tap_stream_id, {}).keys()
                         if non_whitelisted_bookmark_key not in bookmark_key_set]:
        singer.clear_bookmark(state, tap_stream_id, bookmark_key)


def get_new_batch_file_path(table_name, file_index, base_path=DEFAULT_FAST_SYNC_PATH):
    file_name = table_name + '_' + str(file_index).zfill(6) + '.jsonl'
    base_path = os.path.join(
        base_path, table_name
    )
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    return os.path.join(base_path, file_name)


def update_bookmark(record_message, replication_method, catalog_entry, state):
    if replication_method in ('FULL_TABLE', 'LOG_BASED'):

        key_properties = get_key_properties(catalog_entry)
        max_pk_values = singer.get_bookmark(
            state, catalog_entry.tap_stream_id, 'max_pk_values'
        )
        if max_pk_values:
            last_pk_fetched = {
                k:v for k, v in record_message.record.items()
                if k in key_properties
            }
            state = singer.write_bookmark(
                state, catalog_entry.tap_stream_id,
                'last_pk_fetched', last_pk_fetched
            )

    elif replication_method == 'INCREMENTAL':

        replication_key = singer.get_bookmark(
            state, catalog_entry.tap_stream_id, 'replication_key'
        )
        if replication_key is not None:
            state = singer.write_bookmark(
                state, catalog_entry.tap_stream_id,
                'replication_key', replication_key
            )

            state = singer.write_bookmark(
                state, catalog_entry.tap_stream_id,
                'replication_key_value', record_message.record[replication_key]
            )
    return state


def sync_query(config, cursor, catalog_entry, state, select_sql, columns, stream_version, params):

    query_string = cursor.mogrify(select_sql, params)
    time_extracted = utils.now()

    LOGGER.info('Running %s', query_string)
    cursor.execute(select_sql, params)

    database_name = get_database_name(catalog_entry)
    md_map = metadata.to_map(catalog_entry.metadata)
    stream_metadata = md_map.get((), {})
    replication_method = stream_metadata.get('replication-method')
    batch = config.get('batch_messages', False)

    with metrics.record_counter(None) as counter:
        counter.tags['database'] = database_name
        counter.tags['table'] = catalog_entry.table

        rows_saved = 0
        if not batch:

            row = cursor.fetchone()
            while row:
                # Write row
                counter.increment()
                rows_saved += 1
                record_message = row_to_singer_record(
                    catalog_entry,
                    stream_version,
                    row,
                    columns,
                    time_extracted
                )
                singer.write_message(record_message)

                # Update bookmark
                state = update_bookmark(record_message, replication_method, catalog_entry, state)

                if rows_saved % 1000 == 0:
                    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                row = cursor.fetchone()

        else:
            record_message = None
            batch_cursor_size = config.get('batch_cursor_size', 500000)  # Rows to read from cursor
            write_batch_rows = config.get('batch_size', batch_cursor_size * 2)  # rows to write to file
            batch_rows_saved = 0
            batch_file_index = 0

            # Open first file
            tic = time.clock()
            file_path = get_new_batch_file_path(catalog_entry.table, batch_file_index)
            file = open(file_path, 'w')
            batch_file_index += 1

            rows = cursor.fetchmany(batch_cursor_size)
            full_batch = (len(rows) == batch_cursor_size)
            while rows:
                # Write records to json lines file
                for row in rows:
                    record_message = row_to_singer_record(
                        catalog_entry, stream_version,
                        row, columns, time_extracted
                    )
                    file.write(json.dumps(record_message.asdict()))
                    file.write('\n')
                    # Increment counters
                    counter.increment()
                    rows_saved += 1
                    batch_rows_saved += 1
                    state = update_bookmark(record_message, replication_method, catalog_entry, state)

                # If we have reached our write_batch_rows limit,
                # start a new file emit the BATCH RECORD singer message
                # and update the bookmarks
                if batch_rows_saved % write_batch_rows == 0:
                    # close old file
                    file.close()
                    time_taken = time.clock() - tic
                    LOGGER.info(f"{batch_rows_saved} records written to file '{file_path}' in {time_taken}s")
                    # Write batch record
                    singer.write_message(
                        singer.BatchMessage(
                            stream=catalog_entry.stream,
                            filepath=file_path,
                            batch_size=write_batch_rows
                        )
                    )
                    # start a new file
                    tic = time.clock()
                    file_path = get_new_batch_file_path(catalog_entry.table, batch_file_index)
                    file = open(file_path, 'w')
                    batch_file_index += 1
                    # Reset batch row counter
                    batch_rows_saved = 0
                    # write bookmark
                    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                rows = cursor.fetchmany(batch_cursor_size)
                full_batch = (len(rows) == batch_cursor_size)

            # close last file if not already
            if not file.closed:
                file.close()

            # Publish last message, if not already
            if batch_rows_saved % write_batch_rows != 0:
                time_taken = time.clock() - tic
                LOGGER.info(f"{batch_rows_saved} records written to file '{file_path}' in {time_taken}s")
                # Write batch record
                singer.write_message(
                    singer.BatchMessage(
                        stream=catalog_entry.stream,
                        filepath=file_path,
                        batch_size=batch_rows_saved
                    )
                )

    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
