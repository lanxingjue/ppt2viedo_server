# celery_app.py
import os
import sys
from pathlib import Path
import logging # logging 应该在顶层导入

# --- 将项目根目录添加到 sys.path ---
# 这使得 celery_utils 和 app 模块可以被导入
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- 导入 Flask app 实例 ---
# Celery worker 启动时，需要能够访问 Flask app 实例以获取配置
# create_celery_app 函数会处理这个
try:
    from app import app as flask_app_instance # 导入在 app.py 中创建的 Flask app
except ImportError as e:
    logging.critical(f"CRITICAL: celery_app.py 无法导入 Flask app 实例 'app' from 'app.py': {e}. Celery worker 可能无法正确启动或运行。", exc_info=True)
    # 如果 Flask app 无法导入，Celery 将无法正确配置
    # 可以尝试创建一个无配置的 Celery 实例以允许 Celery CLI 至少运行，但任务会失败
    from celery import Celery
    celery_app = Celery('ppt2video_tasks_fallback_no_flask')
    # 在这种情况下，后续的 create_celery_app 调用会失败或使用不完整的配置

# --- 导入并创建 Celery 应用 ---
# create_celery_app 函数现在负责创建和配置 Celery 实例
try:
    from celery_utils import create_celery_app
    # 传递 Flask app 实例给 create_celery_app
    # 这确保 Celery 实例使用 Flask app 的配置
    celery_app = create_celery_app(flask_app_instance)
    logging.info("Celery app instance created successfully using create_celery_app.")
except NameError: # 如果 flask_app_instance 未定义 (因为导入失败)
    logging.critical("flask_app_instance 未定义，无法创建 Celery app。请检查 app.py 的导入。")
    # 创建一个最小的 Celery 实例，以便 Celery CLI 不会完全失败
    from celery import Celery
    celery_app = Celery('ppt2video_tasks_critical_fallback')
except Exception as e_create:
    logging.critical(f"CRITICAL: 调用 create_celery_app 时出错: {e_create}", exc_info=True)
    from celery import Celery
    celery_app = Celery('ppt2video_tasks_critical_fallback_general')


# configparser 对象现在应该由 create_celery_app 从 flask_app.config['APP_CONFIG'] 获取
# 并存储在 celery_app.conf.APP_CONFIG 中。
# 如果需要直接在 celery_app.py 中访问 configparser 对象（例如，在 create_celery_app 之外）：
# from app import config as app_module_config # 如果 app.py 中有全局的 configparser 对象
# 或者重新加载：
# import configparser
# config = configparser.ConfigParser()
# config_path = PROJECT_ROOT / 'config.ini'
# if config_path.exists(): config.read(config_path, encoding='utf-8')
