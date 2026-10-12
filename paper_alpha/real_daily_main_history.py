"""Historical main-producer view from immutable host pins, no current grants."""
from contextlib import closing

from .server.db import connect
from .server.real_daily_main_execution import verify_saved_main_result
from .server.service import ServiceError


def read_main_history(store, run_id):
    with closing(connect(store.db_path)) as connection:
        connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
        run=store._one(connection,'runs',run_id)
        try:
            verified=verify_saved_main_result(store,connection,run)
        except (OSError,ValueError,TypeError,KeyError) as exc:
            raise ServiceError('Main historical producer integrity failed',409) from exc
        return {'schema_version':1,'run_id':run_id,'attempt_id':run['attempt_id'],
            'integrity':'verified','current_permission':'not_checked_history_only',
            'recomputed':False,'model_calls':0,'current_raw_reads':0,**verified}
