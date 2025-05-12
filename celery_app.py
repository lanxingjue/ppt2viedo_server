# celery_app.py
import os
import sys
from pathlib import Path
import logging

# --- 1. 将项目根目录添加到 sys.path ---
# 这必须在尝试导入项目内的任何其他模块之前完成
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
    # 使用 logging 模块进行日志记录，确保它在顶层被导入和配置（如果需要早期日志）
    # logging.basicConfig(level=logging.INFO) # 简单的基础配置
    # logging.info(f"项目根目录 {PROJECT_ROOT} 已添加到 sys.path (from celery_app.py)")

# --- 2. 导入 Flask app 实例 ---
# 这是为了将 Flask app 的配置传递给 Celery
flask_app_for_celery = None
FLASK_APP_LOADED = False
try:
    from app import app as flask_app_imported_for_celery # 从 app.py 导入 Flask app 实例
    flask_app_for_celery = flask_app_imported_for_celery
    FLASK_APP_LOADED = True
    logging.info("celery_app.py: Flask app 实例已成功导入。")
except ImportError as e:
    logging.critical(f"CRITICAL: celery_app.py 无法导入 Flask app 实例 'app' from 'app.py': {e}. Celery 将使用备用配置。", exc_info=True)
except Exception as e_app_import: # 更通用的捕获
    logging.critical(f"CRITICAL: celery_app.py 导入 Flask app 时发生未知错误: {e_app_import}", exc_info=True)


# --- 3. 导入并创建 Celery 应用实例 ---
# celery_app 变量是 Celery CLI (-A celery_app) 寻找的
celery_app = None 
try:
    from celery_utils import create_celery_app # 从 celery_utils.py 导入工厂函数
    if FLASK_APP_LOADED and flask_app_for_celery is not None:
        celery_app = create_celery_app(flask_app_for_celery) # 使用 Flask app 创建 Celery 实例
        logging.info(f"Celery app instance '{celery_app.main if celery_app else 'N/A'}' created by celery_app.py using Flask app config.")
    else:
        logging.warning("celery_app.py: 由于 Flask app 导入失败，将创建使用默认/硬编码配置的 Celery 实例。")
        from celery import Celery
        import configparser
        _config = {}
        try:
            _cp = configparser.ConfigParser()
            if (PROJECT_ROOT / 'config.ini').exists():
                _cp.read(PROJECT_ROOT / 'config.ini', encoding='utf-8')
                _config['broker_url'] = _cp.get('Celery', 'broker_url', fallback='redis://localhost:6379/0')
                _config['result_backend'] = _cp.get('Celery', 'result_backend', fallback='redis://localhost:6379/0')
                _config['APP_CONFIG'] = _cp 
                logging.info("celery_app.py: Fallback Celery config loaded from config.ini")
            else: raise FileNotFoundError("config.ini not found for fallback Celery config")
        except Exception as e_conf_fallback:
            logging.error(f"celery_app.py: Fallback Celery config loading failed: {e_conf_fallback}")
            _config['broker_url'] = 'redis://localhost:6379/0'
            _config['result_backend'] = 'redis://localhost:6379/0'
        celery_app = Celery('ppt2video_tasks_fallback_setup_celery_app', broker=_config.get('broker_url'), backend=_config.get('result_backend'))
        if 'APP_CONFIG' in _config: celery_app.conf.APP_CONFIG = _config['APP_CONFIG']

except ImportError as e_cu:
    logging.critical(f"CRITICAL: celery_app.py 无法导入 'create_celery_app' from 'celery_utils.py': {e_cu}. Celery worker 将无法正确启动。", exc_info=True)
    from celery import Celery
    celery_app = Celery('ppt2video_tasks_utils_import_failed_celery_app') 
except Exception as e_create:
    logging.critical(f"CRITICAL: celery_app.py 调用 create_celery_app 时出错: {e_create}", exc_info=True)
    from celery import Celery
    celery_app = Celery('ppt2video_tasks_create_failed_celery_app')


# --- 4. 在 Celery app 实例创建并赋值给 celery_app 变量后，才导入 tasks 模块 ---
# 这样 tasks.py 中的 @celery_app.task 装饰器就能绑定到上面定义的 celery_app 实例
if celery_app is not None:
    try:
        import tasks # 这会执行 tasks.py，并注册任务到此 celery_app 实例
        logging.info("celery_app.py: tasks 模块已导入，任务应已注册到 celery_app 实例。")
    except ImportError as e_tasks_import:
        logging.error(f"celery_app.py: 导入 tasks 模块失败: {e_tasks_import}", exc_info=True)
    except Exception as e_tasks_general: # 更通用的捕获
        logging.error(f"celery_app.py: 导入 tasks 模块时发生未知错误: {e_tasks_general}", exc_info=True)
else:
    logging.critical("celery_app.py: celery_app 实例未成功创建或为 None，无法导入和注册任务。")

