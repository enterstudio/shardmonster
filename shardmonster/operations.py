"""Contains everything to do with making Mongo operations work across multiple
clusters.
"""
import bson
import numbers
import time

from shardmonster.connection import get_connection, parse_location
from shardmonster.metadata import (
    _get_shards_coll, ShardStatus, _get_realm_for_collection,
    _get_location_for_shard, _get_all_locations_for_realm,
    _get_metadata_for_shard)


def _create_collection_iterator(collection_name, query, with_options={}):
    """Creates an iterator that returns collections and queries that can then
    be used to perform multishard operations:

        for collection, query in _create_collection_iterator(...):
            for doc in collection.find(query):
                yield doc

    This does all the hardwork of figuring out what collections to query and how
    to adjust the query to account for any shards that are currently moving.
    """
    realm = _get_realm_for_collection(collection_name)
    shard_field = realm['shard_field']

    shard_key = _get_query_target(collection_name, query)
    if shard_key:
        location = _get_location_for_shard(realm, shard_key)
        locations = {location.location: location}
    else:
        locations = _get_all_locations_for_realm(realm)

    for location, location_meta in locations.iteritems():
        cluster_name, database_name = parse_location(location)
        connection = get_connection(cluster_name)
        collection = connection[database_name][collection_name]
        if with_options:
            collection = collection.with_options(**with_options)
        if location_meta.excludes:
            if len(location_meta.excludes) == 1:
                query = {'$and': [
                    query, {shard_field: {'$ne': location_meta.excludes[0]}}]}
            else:
                raise Exception('Multiple shards in transit. Aborting')
        yield collection, query
        if location_meta.excludes:
            query = query['$and'][0]


class MultishardCursor(object):
    def __init__(
            self, collection_name, query, *args, **kwargs):
        self.query = query
        self.collection_name = collection_name
        self.args = args
        self.kwargs = kwargs
        self._hint = kwargs.pop('_hint', None)
        self.with_options = kwargs.pop('with_options', {})
        self._prepared = False


    def _create_collection_iterator(self):
        return _create_collection_iterator(
            self.collection_name, self.query, self.with_options)


    def _prepare_for_iteration(self):
        # The multishard cursor has to keep track of a surprising amount of
        # state. When we want to evaluate a multishard cursor the list of
        # queries that need to be performed (and against which collection) is
        # created. This is then used as a basis of iteration and the fact that
        # cursors are changed during iterations is largely not obvious to the
        # end user of this MultishardCursor.
        self._queries_pending = list(self._create_collection_iterator())
        self._cached_results = None
        self._next_cursor()
        self._prepared = True


    def _next_cursor(self):
        collection, query = self._queries_pending.pop(0)
        cursor = collection.find(query, *self.args, **self.kwargs)
        if self._hint:
            cursor = cursor.hint(self._hint)
        self._current_cursor = cursor


    def __iter__(self):
        return self


    def __len__(self):
        return self.count()


    def next(self):
        res = self._next()
        return res


    def _next(self):
        if not self._prepared:
            self.evaluate()

        if self._cached_results:
            return self._cached_results.pop(0)

        try:
            return self._current_cursor.next()
        except StopIteration:
            if self._queries_pending:
                self._next_cursor()
                return self.next()
            else:
                raise


    def limit(self, limit):
        self.kwargs['limit'] = limit
        return self


    def sort(self, key_or_list, direction=None):
        if direction:
            self.kwargs['sort'] = [(key_or_list, direction)]
        else:
            self.kwargs['sort'] = key_or_list
        return self


    def __getitem__(self, i):
        if isinstance(i, int):
            if i != 0:
                raise Exception('Non-zero indexing not currently supported')
            new_kwargs = self.kwargs.copy()
            new_kwargs['limit'] = 1
            new_cursor = MultishardCursor(
                self.collection_name, self.query, _hint=self._hint,
                *self.args, **new_kwargs)
            return list(new_cursor)[0]
        else:
            new_kwargs = self.kwargs.copy()
            new_kwargs['skip'] = i.start or 0
            if i.stop:
                new_kwargs['limit'] = i.stop - (i.start or 0)
            elif 'limit' in new_kwargs:
                del new_kwargs['limit']

            return MultishardCursor(
                self.collection_name, self.query, _hint=self._hint,
                *self.args, **new_kwargs)


    def evaluate(self):
        self._prepare_for_iteration()
        if 'sort' in self.kwargs:
            # Note: This is quite inefficient. In an ideal world this would pass
            # the sort through to each cluster and do the sort at that end and
            # then do a merge sort to save on memory. However, that is more
            # complex and I'd rather this was 100% correct and bloated
            # in memory.
            all_results = list(self)
            def comparator(d1, d2):
                for key, sort_order in self.kwargs['sort']:
                    if d1[key] < d2[key]:
                        return -sort_order
                    elif d1[key] > d2[key]:
                        return sort_order
                return 0
                
            self._cached_results = list(sorted(all_results, cmp=comparator))

        if 'limit' in self.kwargs:
            # Note: This is also inefficient. This gets back all the results and
            # then applies the limit. Again, correctness over efficiency.
            self._cached_results = list(self)[:self.kwargs['limit']]


    def count(self, **count_kwargs):
        total = 0
        for collection, query in self._create_collection_iterator():
            cursor = collection.find(query, *self.args, **self.kwargs)
            if self._hint:
                cursor = cursor.hint(self._hint)
            total += cursor.count(**count_kwargs)
        if self.kwargs.get('limit'):
            return min(self.kwargs['limit'], total)
        else:
            return total


    def rewind(self):
        self._cached_results = None
        self._current_cursor = None
        self._queries_pending = None
        self._prepared = False


    def hint(self, index):
        self._hint = index
        return self


    @property
    def alive(self):
        # Alive has to check the current cursor that is being used - if the
        # current user is not alive then there is a chance that the next cursor
        # could be alive and so we must move onto the next cursor and do
        # the check again.
        if not self._prepared:
            self.evaluate()
        current_alive = self._current_cursor.alive or self._cached_results
        if not current_alive and self._queries_pending:
            self._next_cursor()
            return self.alive

        return current_alive


def _create_multishard_iterator(collection_name, query, *args, **kwargs):
    return MultishardCursor(collection_name, query, *args, **kwargs)


def multishard_find(collection_name, query, *args, **kwargs):
    if 'skip' in kwargs:
        raise Exception('Skip not supported on multishard finds')

    return _create_multishard_iterator(collection_name, query, *args, **kwargs)


def multishard_find_one(collection_name, query, **kwargs):
    kwargs['limit'] = 1
    cursor = _create_multishard_iterator(collection_name, query, **kwargs)
    try:
        return cursor.next()
    except StopIteration:
        return None


def multishard_insert(
        collection_name, doc_or_docs, with_options={}, *args, **kwargs):
    # TODO Remove this and use insert_one/insert_many to comply with new
    # pymongo deprecations
    is_multi_insert = isinstance(doc_or_docs, list)
    if not is_multi_insert:
        all_docs = [doc_or_docs]
    else:
        all_docs = doc_or_docs

    _wait_for_pause_to_end(collection_name, doc_or_docs)
    realm = _get_realm_for_collection(collection_name)
    shard_field = realm['shard_field']
    for doc in all_docs:
        if shard_field not in doc:
            raise Exception(
                'Cannot insert document without shard field (%s) present'
                % shard_field)

    # Inserts can use our generic collection iterator with a specific query
    # that is guaranteed to return exactly one collection.
    # TODO This makes a multi-insert into lots of small inserts. This could be
    # optimised. For now, we'll see if this is OK.
    result = []
    for doc in all_docs:
        simple_query = {shard_field: doc[shard_field]}
        (collection, _), = _create_collection_iterator(
            collection_name, simple_query, with_options)
        result.append(collection.insert(doc, *args, **kwargs))
    if not is_multi_insert:
        return result[0]
    return result


def _is_valid_type_for_sharding(value):
    return isinstance(value, (numbers.Integral, basestring, bson.ObjectId))


def _get_query_target(collection_name, query):
    """Gets out the targetted shard key from the query if there is one.
    Otherwise, returns None.
    """
    realm = _get_realm_for_collection(collection_name)
    shard_field = realm['shard_field']

    if shard_field in query and _is_valid_type_for_sharding(query[shard_field]):
        return query[shard_field]
    return None


def _should_pause_write(collection_name, query):
    realm = _get_realm_for_collection(collection_name)

    shard_key = _get_query_target(collection_name, query)
    if shard_key:
        meta = _get_metadata_for_shard(realm, shard_key)
        return \
            meta['status'] == ShardStatus.POST_MIGRATION_PAUSED_AT_DESTINATION
    else:
        paused_query = {
            'realm': realm['name'],
            'status': ShardStatus.POST_MIGRATION_PAUSED_AT_DESTINATION
        }
        shards_coll = _get_shards_coll()
        return shards_coll.find(paused_query).count() > 0


def _wait_for_pause_to_end(collection_name, query):
    while _should_pause_write(collection_name, query):
        time.sleep(0.05)


def _get_collection_for_targetted_upsert(
        collection_name, query, update, with_options={}):
    shard_key = _get_query_target(collection_name, update['$set'])
    realm = _get_realm_for_collection(collection_name)
    location = _get_location_for_shard(realm, shard_key)

    cluster_name, database_name = parse_location(location.location)
    connection = get_connection(cluster_name)
    collection = connection[database_name][collection_name]
    if with_options:
        collection = collection.with_options(with_options)
    return collection


def multishard_update(collection_name, query, update, with_options={}, **kwargs):
    _wait_for_pause_to_end(collection_name, query)
    overall_result = None
    # If this is an upsert then we check the update to see if it might contain
    # the shard key and use that for the collection iterator. Otherwise,
    # we can end up doing an upsert against all clusters... which results in lots
    # of documents all over the place.
    if (kwargs.get('upsert', False) and '$set' in update and
        _get_query_target(collection_name, update['$set'])):
        # Can't use the normal collection iteration method as it would use the
        # wrong query. Instead, get a specific collection and turn it into the
        # right format.
        collection = _get_collection_for_targetted_upsert(
            collection_name, query, update, with_options)
        collection_iterator = [(collection, query)]
    else:
        collection_iterator = _create_collection_iterator(
            collection_name, query, with_options)

    for collection, targetted_query in collection_iterator:
        result = collection.update(targetted_query, update, **kwargs)
        if not overall_result:
            overall_result = result
        else:
            overall_result['n'] += result['n']

    return overall_result


def multishard_remove(collection_name, query, with_options={}, **kwargs):
    _wait_for_pause_to_end(collection_name, query)
    overall_result = None
    collection_iterator = _create_collection_iterator(
        collection_name, query, with_options)
    for collection, targetted_query in collection_iterator:
        result = collection.remove(targetted_query, **kwargs)
        if not overall_result:
            overall_result = result
        else:
            overall_result['n'] += result['n']

    return overall_result


def multishard_aggregate(
        collection_name, pipeline, with_options={}, *args, **kwargs):
    realm = _get_realm_for_collection(collection_name)
    shard_field = realm['shard_field']
    if '$match' not in pipeline[0]:
        raise Exception(
            'Sharded aggregation needs match in the first part of the pipeline')
    if shard_field not in pipeline[0]['$match']:
        raise Exception(
            'Cannot perform aggregation without shard field (%s) present'
            % shard_field)

    # To avoid aggregation needing to be recreated in this client we limit
    # aggregation to only one cluster.
    match_query = pipeline[0]['$match']
    (collection, _), = _create_collection_iterator(
        collection_name, match_query, with_options)

    # TODO: useCursor needs to be False until support for Mongo2.4 is removed
    return collection.aggregate(pipeline, useCursor=False, *args, **kwargs)


def multishard_save(collection_name, doc, with_options={}, *args, **kwargs):
    _wait_for_pause_to_end(collection_name, doc)
    realm = _get_realm_for_collection(collection_name)
    shard_field = realm['shard_field']
    if shard_field not in doc:
        raise Exception(
            'Cannot save document without shard field (%s) present'
            % shard_field)

    # Inserts can use our generic collection iterator with a specific query
    # that is guaranteed to return exactly one collection.
    simple_query = {shard_field: doc[shard_field]}
    (collection, _), = _create_collection_iterator(
        collection_name, simple_query, with_options)

    return collection.save(doc, *args, **kwargs)


def multishard_ensure_index(collection_name, *args, **kwargs):
    collection_iterator = _create_collection_iterator(collection_name, {})

    for collection, _ in collection_iterator:
        collection.ensure_index(*args, **kwargs)
