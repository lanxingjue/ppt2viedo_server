# celery_utils.py
from celery import Celery
# app.py 中的 Flask app 实例将在这里被导入和使用
# 但为了避免在顶层直接导入 app (可能导致循环)，我们将在函数内部导入

def create_celery_app(flask_app=None):
    """
    创建并配置一个 Celery 应用实例。
    如果提供了 Flask app 实例，则将其配置传递给 Celery。
    """
    if flask_app is None:
        # 如果在 Celery worker 启动时 (celery_app.py 调用) 没有直接传递 flask_app,
        # 我们需要能够导入它。这依赖于 celery_app.py 中对 sys.path 的修改。
        from app import app as current_flask_app # 导入 Flask app
        flask_app = current_flask_app

    # 从 Flask app 的配置中获取 Celery broker 和 backend URL
    # 这要求 Flask app 配置中已经有这些值 (例如从 config.ini 加载)
    broker_url = flask_app.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    result_backend = flask_app.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    
    # 从 Flask app 的配置中获取 configparser 对象 (如果已存储)
    # 这是为了让 Celery 任务可以访问原始的 config.ini 内容
    app_config_parser = flask_app.config.get('APP_CONFIG')

    celery = Celery(
        flask_app.import_name, # 使用 Flask app 的 import_name 作为 Celery app 的名称
        broker=broker_url,
        backend=result_backend,
        include=['tasks'] # 指定包含任务的模块
    )

    # 将 Flask app 的配置更新到 Celery 的配置中
    celery.conf.update(flask_app.config)
    
    # 如果 app_config_parser 存在，也将其存入 Celery 配置
    if app_config_parser:
        celery.conf.APP_CONFIG = app_config_parser
    else:
        # 作为备用，尝试从 celery_app.py 加载（如果那里有）
        try:
            from celery_app import config as celery_app_config_parser
            celery.conf.APP_CONFIG = celery_app_config_parser
            logging.info("Celery 使用了 celery_app.py 中加载的 configparser 对象。")
        except (ImportError, AttributeError):
            logging.warning("Celery 无法从 Flask app 或 celery_app.py 获取 configparser 对象。")


    # 定义一个 Celery Task 基类，它会自动推送 Flask 应用上下文
    class ContextTask(celery.Task):
        abstract = True
        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return super().__call__(*args, **kwargs)

    celery.Task = ContextTask # 将自定义的 Task 类设为 Celery 的默认 Task 类
    return celery

# 在 celery_app.py 中，我们将导入这个函数来创建 celery 实例
