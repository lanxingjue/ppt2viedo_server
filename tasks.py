# tasks.py
import os
import logging
from celery import Celery, signals # signals 用于 worker_init
from celery.utils.log import get_task_logger
# from celery import current_app as celery_current_app # Celery 的 current_app
from pathlib import Path
import configparser
import shutil
import time
import sys
import traceback
from datetime import datetime

# --- 从 celery_utils 导入 Celery 应用实例 ---
# celery_utils.py 应该负责创建和配置 celery_app 实例
try:
    from celery_utils import celery_app # celery_app 是在 celery_utils.py 中创建的 Celery 实例
    # celery_app.conf.APP_CONFIG 应该包含了解析后的 configparser 对象
    # flask_app_for_context = celery_app.flask_app # 如果在 celery_utils 中设置了 flask_app 属性
except ImportError:
    logging.critical("CRITICAL: 无法从 celery_utils 导入 celery_app 实例。任务将无法注册或执行。")
    # 创建一个临时的，以允许文件至少能被解析，但 worker 会失败
    celery_app = Celery('ppt2video_tasks_fallback_tasks_py')

# --- 动态导入 Flask app 和 db 以在任务上下文中使用 ---
# 这些变量将在任务执行时，在 app_context 内被实际赋值和使用
_flask_app_instance = None
_db_instance = None
_User_model = None
_TaskRecord_model = None
_app_config_parser = None # 用于存储解析的 configparser 对象

def get_flask_app_components():
    """辅助函数，用于在需要时加载 Flask app 组件。"""
    global _flask_app_instance, _db_instance, _User_model, _TaskRecord_model, _app_config_parser
    if _flask_app_instance is None: # 只加载一次
        try:
            from app import app as flask_app_imported, db as db_imported
            from models import User as User_imported, TaskRecord as TaskRecord_imported
            
            _flask_app_instance = flask_app_imported
            _db_instance = db_imported
            _User_model = User_imported
            _TaskRecord_model = TaskRecord_imported
            _app_config_parser = _flask_app_instance.config.get('APP_CONFIG') # 从 Flask app 配置获取
            
            if _app_config_parser is None: # 备用：尝试从 Celery conf 获取
                _app_config_parser = celery_app.conf.get('APP_CONFIG')

            if _app_config_parser is None:
                 logging.error("CRITICAL: ConfigParser 对象在 Flask app.config 和 Celery conf 中均未找到！")
            
            logging.info("tasks.py: Flask app, db, models, 和 app_config 已成功按需加载。")
            return True
        except ImportError as e:
            logging.critical(f"CRITICAL: tasks.py 无法按需导入 Flask app, db, 或 models: {e}", exc_info=True)
            return False
        except Exception as e_load:
            logging.critical(f"CRITICAL: tasks.py 按需加载 Flask 组件时发生未知错误: {e_load}", exc_info=True)
            return False
    return True # 如果已加载，则返回 True


# --- 核心逻辑导入 ---
try:
    from core_logic.ppt_processor import process_presentation_for_task
    from core_logic.video_synthesizer import synthesize_video_for_task
    from core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入核心逻辑模块: {e}", exc_info=True)

task_logger = get_task_logger(__name__)

# --- 任务阶段常量 ---
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video'
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'
STAGE_DB_UPDATE = 'Updating Database'


@celery_app.task(bind=True, name='ppt_to_video.convert_task', acks_late=True, reject_on_worker_lost=True,
                  time_limit=3600, soft_time_limit=3500)
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str, 
                              task_record_id: int, user_id: int):
    task_celery_id = self.request.id
    logger = task_logger
    logger.info(f"Celery 任务 {task_celery_id} (DB Record ID: {task_record_id}) 开始，用户: {user_id}, 文件: {Path(pptx_filepath_str).name}")
    start_time = time.time()
    temp_run_dir = None
    final_video_relative_path = None

    # 确保 Flask 组件已加载
    if not get_flask_app_components() or not _flask_app_instance:
        error_msg = "任务启动失败：Flask 应用上下文或数据库/模型/配置不可用。"
        logger.error(f"任务 {task_celery_id}: {error_msg}")
        self.update_state(state='FAILURE', meta={'error': error_msg, 'stage': STAGE_START, 'exc_type': 'SetupError'})
        return

    # 使用 with _flask_app_instance.app_context()
    with _flask_app_instance.app_context():
        task_record = None
        try:
            task_record = _db_instance.session.get(_TaskRecord_model, task_record_id)
            if not task_record:
                error_msg = f'数据库记录 TaskRecord ID {task_record_id} 未找到。'
                logger.error(f"任务 {task_celery_id}: {error_msg}")
                self.update_state(state='FAILURE', meta={'error': error_msg, 'stage': STAGE_START, 'exc_type': 'DBRecordNotFound'})
                return

            if task_record.celery_task_id != task_celery_id and task_record.celery_task_id.startswith("TEMP_"):
                 task_record.celery_task_id = task_celery_id
            
            task_record.status = 'PROCESSING'
            _db_instance.session.commit()
            logger.info(f"任务 {task_celery_id}: TaskRecord {task_record_id} 状态更新为 PROCESSING。")
            self.update_state(state='STARTED', meta={'stage': STAGE_START, 'progress': 0})

            current_task_config = _app_config_parser # 使用已加载的 configparser 对象
            if not current_task_config:
                raise RuntimeError("ConfigParser 对象在任务执行时不可用。")

            base_temp_dir = Path(current_task_config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
            base_temp_dir.mkdir(parents=True, exist_ok=True)

            pptx_filepath = Path(pptx_filepath_str)
            output_base_path = Path(output_dir_str)
            safe_original_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in pptx_filepath.stem)
            
            # 使用 TaskRecord ID 和部分 Celery ID 确保文件名唯一
            unique_video_filename = f"{safe_original_stem}_task{task_record_id}_{task_celery_id[:8]}.mp4"
            final_video_full_path = output_base_path / unique_video_filename
            final_video_relative_path = unique_video_filename

            task_record.output_video_filename = final_video_relative_path
            # original_ppt_path 应该在 app.py 中创建 TaskRecord 时就已保存
            # task_record.original_ppt_path = # 不需要在这里设置，app.py 中已处理
            _db_instance.session.commit()

            self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 10})
            processed_data, temp_run_dir = process_presentation_for_task(
                pptx_filepath, base_temp_dir, voice_id, logger, current_task_config, self
            )
            self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 45})

            self.update_state(state='PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'progress': 50})
            synthesis_success = synthesize_video_for_task(
                processed_data, temp_run_dir, final_video_full_path, logger, current_task_config, self
            )

            if not synthesis_success:
                raise RuntimeError("视频合成步骤返回失败。")

            end_time = time.time()
            duration = end_time - start_time
            logger.info(f"任务 {task_celery_id} 成功！输出: {final_video_full_path.resolve()}，耗时: {duration:.2f}s")
            self.update_state(state='PROCESSING', meta={'stage': STAGE_DB_UPDATE, 'progress': 95, 'status': '更新数据库记录...'})
            
            task_record.status = 'SUCCESS'
            task_record.completed_at = datetime.utcnow()
            
            user = _db_instance.session.get(_User_model, user_id)
            if user:
                if user.role == 'free': # 仅对免费用户增加计数
                    user.increment_video_count()
                _db_instance.session.add(user)
                logger.info(f"用户 {user.username} ({user.role}) 视频计数更新为: {user.videos_created_count}")
            else:
                logger.warning(f"任务 {task_celery_id}: 未找到用户 ID {user_id} 来更新视频计数。")
            
            _db_instance.session.commit()
            logger.info(f"任务 {task_celery_id}: TaskRecord {task_record_id} 和 User {user_id} 已更新。")

            self.update_state(state='SUCCESS', meta={
                'stage': STAGE_COMPLETE, 'progress': 100,
                'output_path': str(final_video_relative_path), 'duration': duration
            })
            return str(final_video_relative_path)

        except Exception as e:
            detailed_traceback = traceback.format_exc()
            error_msg_for_user = f"任务处理失败: {type(e).__name__} - {str(e)[:200]}"
            error_msg_for_db = f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{detailed_traceback}"
            logger.error(f"任务 {task_celery_id} (DB Record: {task_record_id}) 失败: {e}", exc_info=True)

            current_meta_for_celery = {}
            try:
                current_meta_for_celery = self.request.get_current_task().info or {}
            except Exception:
                logger.warning(f"任务 {task_celery_id}: 获取当前任务 meta 失败。")

            self.update_state(state='FAILURE', meta={
                **current_meta_for_celery,
                'error': error_msg_for_user,
                'exc_type': type(e).__name__,
                'traceback': detailed_traceback,
                'stage': current_meta_for_celery.get('stage', 'Unhandled Error in Task')
            })

            if task_record and _db_instance: # 确保 task_record 和 _db_instance 都有效
                task_record.status = 'FAILURE'
                task_record.error_message = error_msg_for_db[:5000]
                task_record.completed_at = datetime.utcnow()
                try:
                    _db_instance.session.commit()
                except Exception as db_error:
                    logger.error(f"任务 {task_celery_id}: 更新 TaskRecord {task_record_id} 为 FAILURE 时数据库错误: {db_error}", exc_info=True)
                    _db_instance.session.rollback()
            return

        finally:
            logger.info(f"任务 {task_celery_id}: Finally 块开始执行清理。")
            if _app_config_parser: # 确保配置对象存在
                cleanup_temp = _app_config_parser.getboolean('General', 'cleanup_temp_dir', fallback=True)
                if temp_run_dir and temp_run_dir.exists():
                    if cleanup_temp:
                        logger.info(f"任务 {task_celery_id}: 清理临时目录: {temp_run_dir}")
                        try:
                            shutil.rmtree(temp_run_dir)
                            logger.info(f"任务 {task_celery_id}: 临时目录已清理。")
                        except Exception as clean_e:
                            logger.error(f"任务 {task_celery_id}: 清理临时目录 {temp_run_dir} 失败: {clean_e}", exc_info=True)
                    else:
                        logger.info(f"任务 {task_celery_id}: 临时文件保留于: {temp_run_dir} (cleanup_temp_dir=False)")
                elif temp_run_dir:
                     logger.info(f"任务 {task_celery_id}: 临时目录 {temp_run_dir} 未找到，无需清理。")
            else:
                logger.warning(f"任务 {task_celery_id}: 无法获取清理配置，跳过清理。")


# --- 获取可用语音列表的函数 (供 app.py 调用) ---
def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    return get_available_tts_voices_core(logger)


# --- Worker 初始化信号处理 ---
@signals.worker_init.connect
def worker_init_handler(**kwargs):
    worker_logger = logging.getLogger('celery.worker.init')
    worker_logger.info("Celery worker 正在初始化 (tasks.py worker_init_handler)...")
    # 尝试加载 Flask 组件，以便在任务执行前预热或检查
    if not get_flask_app_components():
        worker_logger.critical("Flask 应用组件在 worker 初始化时加载失败。任务可能无法正确执行。")
    else:
        worker_logger.info("Flask 应用组件在 worker 初始化时似乎可按需加载。")
    worker_logger.info("Worker 初始化完成。")
