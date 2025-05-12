# celery_utils.py
import logging
from celery import Celery

logger = logging.getLogger(__name__)

def create_celery_app(flask_app_instance): # 参数名明确表示是 Flask app 实例
    """
    创建并配置一个 Celery 应用实例。
    必须传递有效的 Flask app 实例。
    """
    if flask_app_instance is None:
        logger.critical("CRITICAL: create_celery_app 必须接收一个有效的 Flask app 实例！")
        # 返回一个基础的、可能无法正常工作的 Celery 实例
        return Celery('ppt2video_tasks_critical_flask_app_missing_in_utils')

    # 从 Flask app 的配置中获取 Celery broker 和 backend URL
    broker_url = flask_app_instance.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    result_backend = flask_app_instance.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    
    # 从 Flask app 的配置中获取原始的 configparser 对象
    app_config_parser = flask_app_instance.config.get('APP_CONFIG')

    celery_instance = Celery(
        flask_app_instance.import_name, # 使用 Flask app 的 import_name 作为 Celery app 的名称
        broker=broker_url,
        backend=result_backend
        # `include` 参数不在这里设置，任务的发现由 celery_app.py 中的 `import tasks` 处理
    )

    # 将 Flask app 的配置更新到 Celery 的配置中
    celery_instance.conf.update(flask_app_instance.config)
    
    if app_config_parser:
        celery_instance.conf.APP_CONFIG = app_config_parser # 存储原始 configparser 对象
        logger.info("Celery 配置已更新，并存储了 APP_CONFIG (configparser 对象)。")
    else:
        logger.warning("Celery 配置：未从 Flask app 获取到 APP_CONFIG (configparser 对象)。")

    # 定义一个 Celery Task 基类，它会自动推送 Flask 应用上下文
    class ContextTask(celery_instance.Task):
        abstract = True
        def __call__(self, *args, **kwargs):
            # 使用传递进来的 flask_app_instance 来创建上下文
            with flask_app_instance.app_context():
                return super().__call__(*args, **kwargs)

    celery_instance.Task = ContextTask
    logger.info(f"Celery app '{celery_instance.main}' created with ContextTask using Flask app '{flask_app_instance.name}'.")
    return celery_instance
