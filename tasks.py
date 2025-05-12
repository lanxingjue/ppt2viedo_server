# tasks.py
import os
import logging
from celery import signals
from celery.utils.log import get_task_logger
from pathlib import Path
import shutil
import time
import traceback
from datetime import datetime

# --- 从 celery_app 模块导入 Celery 应用实例 ---
# celery_app.py 负责创建名为 'celery_app' 的 Celery 实例。
CELERY_APP_LOADED = False
celery_app = None # 初始化为 None
try:
    # 关键: 确保这里的 'celery_app' 与 celery_app.py 中定义的 Celery 实例变量名一致
    from celery_app import celery_app as celery_instance_from_main_module
    if celery_instance_from_main_module is not None:
        celery_app = celery_instance_from_main_module
        CELERY_APP_LOADED = True
        logging.info("tasks.py: Celery app instance successfully loaded from 'celery_app' module.")
    else:
        logging.critical("CRITICAL: tasks.py: 从 'celery_app' 模块导入的 'celery_app' 实例为 None。")
        from celery import Celery # Fallback
        celery_app = Celery('tasks_fallback_celery_app_was_none') 
except ImportError as e:
    logging.critical(f"CRITICAL: tasks.py 无法从 'celery_app' 模块导入 'celery_app' 实例: {e}. 任务将无法正确注册或执行。", exc_info=True)
    from celery import Celery # Fallback
    celery_app = Celery('tasks_fallback_celery_app_import_failed_in_tasks_v13')


# --- 核心逻辑导入 ---
CORE_LOGIC_LOADED = False
try:
    from core_logic.ppt_processor import process_presentation_for_task
    from core_logic.video_synthesizer import synthesize_video_for_task
    from core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core
    CORE_LOGIC_LOADED = True
except ImportError as e:
    logging.error(f"FATAL ERROR: tasks.py 无法导入核心逻辑模块: {e}", exc_info=True)

task_logger = get_task_logger(__name__)

# --- 任务阶段常量 ---
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video'
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'
STAGE_DB_UPDATE = 'Updating Database'


# 使用从 celery_app.py 导入的 celery_app 实例来装饰任务
@celery_app.task(bind=True, name='ppt_to_video.convert_task', acks_late=True, reject_on_worker_lost=True,
                  time_limit=3600, soft_time_limit=3500)
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str, 
                              task_record_id: int, user_id: int):
    task_celery_id = self.request.id
    logger = task_logger
    logger.info(f"Celery 任务 {task_celery_id} (DB Record ID: {task_record_id}) 开始，用户: {user_id}, 文件: {Path(pptx_filepath_str).name}")
    
    if not CELERY_APP_LOADED or celery_app is None or celery_app.main == 'tasks_fallback_celery_app_is_none' or celery_app.main == 'tasks_fallback_celery_app_import_failed_in_tasks_v13':
        logger.critical(f"任务 {task_celery_id} 无法执行：Celery app (from celery_app.py) 未正确加载到 tasks.py 或为备用实例。")
        self.update_state(state='FAILURE', meta={'error': 'Celery app not loaded correctly in task module.', 'stage': STAGE_START, 'exc_type': 'SetupError'})
        return
    if not CORE_LOGIC_LOADED:
        logger.critical(f"任务 {task_celery_id} 无法执行：核心处理逻辑模块未正确加载。")
        self.update_state(state='FAILURE', meta={'error': 'Core logic modules not loaded.', 'stage': STAGE_START, 'exc_type': 'SetupError'})
        return

    _db_instance_task = None
    _User_model_task = None
    _TaskRecord_model_task = None
    _config_parser_task = None
    flask_components_loaded_in_task = False

    try:
        # ContextTask (在 celery_utils.py 中定义) 会自动处理 app_context
        from app import db as _db_imported_in_task, app as current_flask_app_in_task_context
        from models import User as _User_imported_in_task, TaskRecord as _TaskRecord_imported_in_task
        
        _db_instance_task = _db_imported_in_task
        _User_model_task = _User_imported_in_task
        _TaskRecord_model_task = _TaskRecord_imported_in_task
        
        _config_parser_task = celery_app.conf.get('APP_CONFIG') 
        if not _config_parser_task:
            logger.warning("tasks.py: APP_CONFIG 未在 celery_app.conf 中找到，尝试从 current_flask_app.config 获取。")
            _config_parser_task = current_flask_app_in_task_context.config.get('APP_CONFIG')

        if not _config_parser_task:
            raise RuntimeError("ConfigParser 对象 (APP_CONFIG) 在 Celery 和 Flask 配置中均未找到。")
        flask_components_loaded_in_task = True
        
    except Exception as e_load:
        logger.critical(f"任务 {task_celery_id}: 无法在任务内部加载 Flask/DB/Model/Config组件: {e_load}", exc_info=True)
        self.update_state(state='FAILURE', meta={'error': 'Task setup failed: Cannot load Flask components.', 'stage': STAGE_START, 'exc_type': 'SetupError'})
        return

    if not flask_components_loaded_in_task:
        logger.critical(f"任务 {task_celery_id}: Flask 组件未能加载，任务中止。")
        return

    start_time = time.time()
    temp_run_dir = None
    final_video_relative_path = None
    task_record = None

    try:
        task_record = _db_instance_task.session.get(_TaskRecord_model_task, task_record_id)
        if not task_record:
            error_msg = f'数据库记录 TaskRecord ID {task_record_id} 未找到。'
            logger.error(f"任务 {task_celery_id}: {error_msg}")
            self.update_state(state='FAILURE', meta={'error': error_msg, 'stage': STAGE_START, 'exc_type': 'DBRecordNotFound'})
            return

        if task_record.celery_task_id != task_celery_id and task_record.celery_task_id.startswith("TEMP_"):
             task_record.celery_task_id = task_celery_id
        
        task_record.status = 'PROCESSING'
        _db_instance_task.session.commit()
        logger.info(f"任务 {task_celery_id}: TaskRecord {task_record_id} 状态更新为 PROCESSING。")
        self.update_state(state='STARTED', meta={'stage': STAGE_START, 'progress': 0})

        current_task_config = _config_parser_task
        base_temp_dir = Path(current_task_config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
        base_temp_dir.mkdir(parents=True, exist_ok=True)

        pptx_filepath = Path(pptx_filepath_str)
        output_base_path = Path(output_dir_str)
        safe_original_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in pptx_filepath.stem)
        
        unique_video_filename = f"{safe_original_stem}_task{task_record_id}_{task_celery_id[:8]}.mp4"
        final_video_full_path = output_base_path / unique_video_filename
        final_video_relative_path = unique_video_filename

        task_record.output_video_filename = final_video_relative_path
        _db_instance_task.session.commit()

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
        
        user = _db_instance_task.session.get(_User_model_task, user_id)
        if user:
            if user.role == 'free':
                user.increment_video_count()
            _db_instance_task.session.add(user)
            logger.info(f"用户 {user.username} ({user.role}) 视频计数更新为: {user.videos_created_count}")
        else:
            logger.warning(f"任务 {task_celery_id}: 未找到用户 ID {user_id} 来更新视频计数。")
        
        _db_instance_task.session.commit()
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
        logger.error(f"任务 {task_celery_id} (DB Record: {task_record_id if task_record else 'N/A'}) 失败: {e}", exc_info=True)

        current_meta_for_celery = {}
        try:
            current_meta_for_celery = self.request.get_current_task().info or {}
        except Exception: logger.warning(f"任务 {task_celery_id}: 获取当前任务 meta 失败。")

        self.update_state(state='FAILURE', meta={
            **current_meta_for_celery,
            'error': error_msg_for_user, 'exc_type': type(e).__name__,
            'traceback': detailed_traceback,
            'stage': current_meta_for_celery.get('stage', 'Unhandled Error in Task')
        })

        if task_record and _db_instance_task:
            task_record.status = 'FAILURE'
            task_record.error_message = error_msg_for_db[:5000]
            task_record.completed_at = datetime.utcnow()
            try: _db_instance_task.session.commit()
            except Exception as db_error:
                logger.error(f"任务 {task_celery_id}: 更新 TaskRecord {task_record_id} 为 FAILURE 时数据库错误: {db_error}", exc_info=True)
                _db_instance_task.session.rollback()
        return

    finally:
        logger.info(f"任务 {task_celery_id}: Finally 块开始执行清理。")
        if _config_parser_task:
            cleanup_temp = _config_parser_task.getboolean('General', 'cleanup_temp_dir', fallback=True)
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
            logger.warning(f"任务 {task_celery_id}: 无法获取清理配置 (_config_parser_task is None)，跳过清理。")


def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    return get_available_tts_voices_core(logger)

@signals.worker_init.connect
def worker_init_handler(**kwargs):
    worker_logger = logging.getLogger('celery.worker.init')
    worker_logger.info("Celery worker 正在初始化 (tasks.py worker_init_handler)...")
    if not CELERY_APP_LOADED:
        worker_logger.critical("Celery app instance (from celery_app module) 在 worker 初始化时未加载。")
    if not CORE_LOGIC_LOADED:
        worker_logger.critical("核心逻辑模块在 worker 初始化时未加载。")
    worker_logger.info("Worker 初始化完成。")

