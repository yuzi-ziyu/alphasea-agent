import time
import threading
import traceback
import pickle
import pandas as pd
from ..prediction_format import validate_content, parse_content
from ..logger import create_null_logger

day_seconds = 24 * 60 * 60


class Executor:
    def __init__(self, store=None, tournament_id=None, time_func=None, evaluation_periods=None,
                 model_selector=None, market_data_store=None, budget_rate=None,
                 symbol_white_list=None, logger=None, redis_client=None):
        self._store = store
        self._tournament = None
        self._tournament_id = tournament_id
        self._time_func = time.time if time_func is None else time_func
        self._interval_sec = 15
        self._logger = create_null_logger() if logger is None else logger
        self._redis_client = redis_client

        self._evaluation_periods = evaluation_periods
        self._model_selector = model_selector
        self._market_data_store = market_data_store
        self._symbol_white_list = symbol_white_list.copy()
        self._budget_rate = budget_rate
        self._thread = None
        self._thread_terminated = False
        self._initialized = False

    # redis

    def _get_purchase_info(self, execution_start_at):
        key = _purchase_info_key(execution_start_at)
        value = self._redis_client.get(key)
        if value is None:
            return None
        return pickle.loads(value)

    def _set_purchase_info(self, execution_start_at, info):
        key = _purchase_info_key(execution_start_at)
        self._redis_client.set(key, pickle.dumps(info))
        self._redis_client.expireat(key, execution_start_at + 2 * 24 * 60 * 60)

    def start_thread(self):
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def terminate_thread(self):
        self._thread_terminated = True
        self._thread.join()

    def get_blended_prediction(self, execution_start_at: int):
        empty_result = pd.DataFrame([], columns=['symbol', 'position']).set_index('symbol')
        purchase_info = self._get_purchase_info(execution_start_at)
        if purchase_info is None:
            return empty_result

        predictions = self._store.fetch_predictions(
            tournament_id=self._tournament_id,
            execution_start_at=execution_start_at
        )
        predictions = pd.DataFrame(predictions)
        predictions = predictions.set_index('model_id')

        df_weight = purchase_info['df_weight']

        dfs = []
        for model_id in df_weight.index:
            try:
                pred = predictions.loc[model_id]
                validate_content(pred['content'])
                df = parse_content(pred['content'])
                df['position'] *= df_weight.loc[model_id, 'weight']
                df = df.reset_index()
                dfs.append(df)
            except Exception as e:
                self._logger.error(e)
                self._logger.error(traceback.format_exc())

        if len(dfs) == 0:
            return empty_result

        df = pd.concat(dfs, axis=1)

        return pd.concat([
            df.groupby('symbol')['position'].sum()
        ], axis=1)

    def _run(self):
        while not self._thread_terminated:
            try:
                if not self._initialized:
                    self._initialize()
                    self._initialized = True
                self._step()
            except Exception as e:
                self._logger.error(e)
                self._logger.error(traceback.format_exc())
            time.sleep(self._interval_sec)

    def _step_purchase(self, execution_start_at):
        purchase_info = self._get_purchase_info(execution_start_at)
        if purchase_info is not None:
            return

        without_fetch_events = False

        # 過去予測を取得
        dfs = []
        for i in range(2, 2 + self._evaluation_periods):
            predictions = self._store.fetch_predictions(
                tournament_id=self._tournament_id,
                execution_start_at=execution_start_at - day_seconds * i,
                without_fetch_events=without_fetch_events,
            )
            without_fetch_events = True
            for prediction in predictions:
                try:
                    validate_content(prediction['content'])
                    df = parse_content(prediction['content'])
                    df['model_id'] = prediction['model_id']
                    df['execution_start_at'] = prediction['execution_start_at']
                    df = df.reset_index().set_index(['model_id', 'execution_start_at', 'symbol'])
                    dfs.append(df)
                except Exception as e:
                    self._logger.error(e)
                    self._logger.error(traceback.format_exc())

        if len(dfs) == 0:
            df = pd.DataFrame(
                [],
                columns=['model_id', 'execution_start_at', 'symbol', 'position']
            ).set_index(['model_id', 'execution_start_at', 'symbol'])
        else:
            df = pd.concat(dfs)
        df = df.sort_index()

        # 今ラウンドで売りに出ている予測取得
        latest_predictions = self._store.fetch_predictions(
            tournament_id=self._tournament_id,
            execution_start_at=execution_start_at
        )
        df_model = pd.DataFrame(latest_predictions, columns=['model_id', 'price', 'locally_stored']).set_index(
            'model_id')
        df_model = df_model.sort_index()
        # メモリにあるモデルの購入費用は0
        df_model.loc[df_model['locally_stored'], 'price'] = 0

        # リターン取得
        df_market = self._market_data_store.fetch_df_market(
            symbols=self._symbol_white_list,
        )

        # モデル選択
        df_weight = self._model_selector.select_model(
            df=df,
            df_model=df_model,
            df_market=df_market,
            budget=self._budget_rate * self._store.get_balance(),
        )
        df_weight = df_weight.loc[df_weight['weight'] > 0]
        if df_weight.shape[0] == 0:
            return

        # 購入
        create_purchase_params_list = []
        for model_id in df_weight.index:
            if df_model.loc[model_id, 'locally_stored']:
                continue
            create_purchase_params_list.append({
                'model_id': model_id,
                'execution_start_at': execution_start_at
            })
        self._store.create_purchases(create_purchase_params_list)

        self._set_purchase_info(execution_start_at, {
            'df_weight': df_weight
        })

    def _step(self):
        now = int(self._time_func())

        t = self._tournament

        purchase_time_buffer = int(t['purchase_time'] * 0.2)
        purchase_start_at = (t['execution_start_at'] - t['execution_preparation_time'] -
                             t['shipping_time'] - t['purchase_time'] + purchase_time_buffer)

        interval = t['execution_time']

        if (now - purchase_start_at) % interval < t['purchase_time'] - purchase_time_buffer:
            execution_start_at = ((now - purchase_start_at) // interval) * interval + t['execution_start_at']
            self._step_purchase(execution_start_at)

    def _initialize(self):
        self._tournament = self._store.fetch_tournament(self._tournament_id)
        self._market_data_store.fetch_df_market(
            symbols=self._symbol_white_list,
        )


def _purchase_info_key(execution_start_at):
    return 'purchase_info:{}'.format(execution_start_at)
