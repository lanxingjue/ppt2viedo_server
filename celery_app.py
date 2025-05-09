# celery_app.py
import os
import sys # 导入 sys
from pathlib import Path # 导入 Path
import logging # 导入 logging
import configparser
from celery import Celery

# --- 将项目根目录添加到 sys.path ---
# 这确保了当 Celery worker 启动并导入 tasks.py 时，
# tasks.py 中的 "from app import ..." 能够找到 app.py 模块。
# 这是解决 ModuleNotFoundError: No module named 'app' 的关键。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# --- sys.path 修改结束 ---

# --- 配置解析 ---
config = configparser.ConfigParser()
config_path = PROJECT_ROOT / 'config.ini' # 使用 PROJECT_ROOT 构建路径

if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        # 使用 logging 模块而不是 print 进行日志记录，更规范
        logging.info(f"[Celery App] 成功加载配置: {config_path}")
    except Exception as e:
        logging.error(f"[Celery App] 错误: 加载配置 {config_path} 失败: {e}")
else:
    logging.warning(f"[Celery App] 警告: 配置未找到: {config_path}")

# --- 日志记录配置 (可选，Celery 有自己的日志) ---
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(process)d] - %(message)s')


# --- 创建 Celery 应用实例 ---
broker_url = config.get('Celery', 'broker_url', fallback='redis://localhost:6379/0')
result_backend = config.get('Celery', 'result_backend', fallback='redis://localhost:6379/0')

celery_app = Celery(
    'ppt2video_tasks', # 应用名，可以与 Flask app 名不同
    broker=broker_url,
    backend=result_backend,
    include=['tasks'] # Celery 会自动发现这个模块中的任务
)

celery_app.conf.update(
    timezone='Asia/Shanghai', # 根据需要调整
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    result_expires=60*60*24, # 任务结果一天后过期
    # task_acks_late = True, # 任务执行成功后才确认，防止 worker 崩溃导致任务丢失
    # worker_prefetch_multiplier = 1, # 每个 worker 一次只取一个任务，如果任务耗时较长
)

# 可选：将解析的 config 对象传递给 Celery 配置，以便任务可以访问
# celery_app.conf.app_config = config # 任务中可以通过 current_app.conf.app_config 访问

# 如果直接运行此文件 (例如 python celery_app.py worker)，下面的代码不会执行
# Worker 通常通过 celery -A celery_app worker ... 命令启动
if __name__ == '__main__':
    # 这通常不会被直接运行以启动 worker
    # celery_app.start() # 这是旧的启动方式
    pass
