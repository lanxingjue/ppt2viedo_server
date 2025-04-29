# celery_app.py
import os
import logging
from celery import Celery
from pathlib import Path
import configparser

# --- 配置解析 ---
# 尝试加载 config.ini 文件
config = configparser.ConfigParser()
# 在 Celery worker 环境中，脚本可能从不同目录启动，
# 使用 __file__ 的父目录来确定 config.ini 的位置比较可靠。
# 假定 config.ini 和 celery_app.py 在同一目录。
config_path = Path(__file__).parent / 'config.ini'
if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        print(f"[Celery App] 成功加载配置: {config_path}") # 启动时打印，确保配置加载
    except Exception as e:
        print(f"[Celery App] 错误: 加载配置 {config_path} 失败: {e}")
        # Celery 启动不应该因为配置加载失败而中断，但会打印错误
else:
    print(f"[Celery App] 警告: 配置未找到: {config_path}")
    # 没有 config.ini，许多默认值将不起作用，可能导致问题

# --- 日志记录配置 ---
# Celery Worker 通常有自己的日志管理，但这里配置基础日志，以防万一
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

# 确保日志记录器在 Celery worker 启动时被正确配置
# Celery 默认也会配置日志，这里可以覆盖或补充
# 注意：直接在这里配置 handler 可能导致重复日志
# 更好的方式是在 task 中获取 Celery logger 并使用
# logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(process)d] - %(message)s')


# --- 创建 Celery 应用实例 ---
# 从 config.ini 的 [Celery] 部分读取 broker 和 backend URL
broker_url = config.get('Celery', 'broker_url', fallback='redis://localhost:6379/0')
result_backend = config.get('Celery', 'result_backend', fallback='redis://localhost:6379/0')

# Celery 应用名可以与项目名一致
celery_app = Celery(
    'ppt2video_tasks',
    broker=broker_url,
    backend=result_backend
)

# 配置时区和任务序列化等
celery_app.conf.update(
    # 时区，根据你的需要调整
    timezone='Asia/Shanghai',
    # 任务序列化方式
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    # 任务结果过期时间（秒），例如一天
    result_expires=60*60*24,
    # 导入包含任务定义的模块
    # 注意：这里指定的是包含 @celery_app.task 装饰器的模块名
    # 假定任务定义在 tasks.py 文件中
    imports=['tasks'],

    # 添加配置对象到配置中，方便在任务中访问
    # 警告：ConfigParser 对象通常不可序列化！
    # 更好的做法是在任务内部重新加载 config 或只传递必要的配置值
    # 这里为了简单，不直接将 config 对象放入 app.conf

    # 如果需要访问 config，可以在任务内部通过 import configparser 和读取 config.ini
    # 或者在任务定义时通过 bind=True 获取 task 实例，并将配置作为参数传递给核心函数
)

# 如果需要，可以在这里配置日志
# 例如，为 Celery Worker 配置日志 handler
# if __name__ != '__main__':
#     # 只有在作为 worker 启动时才配置
#     celery_logger = logging.getLogger('celery')
#     celery_logger.setLevel(log_level)
#     # 可以添加 FileHandler 等