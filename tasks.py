# tasks.py
import os
import logging
# from celery import Celery # 不需要在这里定义 Celery 实例
from celery import signals
from celery.utils.log import get_task_logger # 获取任务专属日志记录器
from pathlib import Path
import configparser
import shutil
import time

# --- 修改: 从 celery_app 文件导入 Celery 应用实例 (现在是绝对导入) ---
# 假定 celery_app.py 文件在项目根目录
# 如果 celery_app.py 在子目录 myapp/celery_app.py，这里应是 from myapp.celery_app import celery_app
from celery_app import celery_app # <--- 改为绝对导入
# --- End 修改 ---

# --- 修改: 导入核心处理逻辑函数 (改为绝对导入) ---
# 假定 core_logic 目录在项目根目录
# 如果 core_logic 在子目录 myapp/core_logic，这里应是 from myapp.core_logic...
try:
    from core_logic.ppt_processor import process_presentation_for_task
    from core_logic.video_synthesizer import synthesize_video_for_task
    from core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core
    from core_logic.utils import get_tool_path, get_poppler_path, get_audio_duration # 导入 utils
except ImportError as e:
    # 如果核心逻辑模块导入失败，整个 worker 将无法启动或任务会失败
    logging.error(f"FATAL ERROR: 无法导入核心逻辑模块或其依赖: {e}")
    # 在任务加载阶段失败，直接抛出异常让 Celery 报告
    raise ImportError(f"无法导入核心逻辑模块或其依赖: {e}") from e
# --- End 修改 ---

# --- 获取任务专属日志记录器 ---
# 这是在 Celery Worker 中进行日志记录的标准方式
task_logger = get_task_logger(__name__)


# --- 定义 Celery 任务 ---
# @celery_app.task 装饰器将一个普通 Python 函数注册为 Celery 任务
# bind=True 表示任务函数将接收第一个参数 'self'，即任务实例，可以访问 retry, request 等属性
# name 可以指定任务名称，不指定则默认使用函数名（包含模块名，如 tasks.convert_ppt_to_video_task）
@celery_app.task(bind=True, name='ppt_to_video.convert_task')
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str):
    """
    后台 Celery 任务：执行 PPT 到视频的转换。

    Args:
        self: 任务实例。
        pptx_filepath_str: 上传的 PPTX 文件路径（字符串）。
        output_dir_str: 最终视频输出目录路径（字符串）。
        voice_id: 选择的 TTS 语音 ID。

    Returns:
        成功时返回最终视频文件相对于基础输出目录的路径（字符串），失败时记录错误并由 Celery 标记失败。
    """
    # --- 获取配置和日志记录器 ---
    # 在任务内部重新加载配置是确保使用最新配置的安全方式
    config = configparser.ConfigParser()
    # config.ini 相对于项目根目录，这里需要找到它
    # 假定任务 Worker 在项目根目录启动，或者项目根目录在 PYTHONPTAH 中
    # 使用 Path(__file__).parent.parent 定位到项目根目录
    config_path = Path(__file__).parent.parent / 'config.ini'
    if config_path.exists():
        try:
            config.read(config_path, encoding='utf-8')
            # task_logger.debug("任务中成功加载配置。") # 使用任务日志
        except Exception as e:
            task_logger.error(f"任务 {self.request.id} 加载配置 {config_path} 失败: {e}")
            # 严重错误，标记任务失败
            raise RuntimeError(f"加载配置失败: {e}")

    logger = task_logger # 使用任务专属日志记录器

    # 从配置获取临时文件基础目录
    # 使用 /tmp/ppt2video_temp 作为 Linux 上的默认回退，或其他适合服务器的环境
    base_temp_dir = Path(config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
    # 确保临时目录基础路径存在
    try:
         # 确保在任务 Worker 运行的用户下有权限创建此目录
         base_temp_dir.mkdir(parents=True, exist_ok=True)
         logger.info(f"任务 {self.request.id} 临时文件基础目录: {base_temp_dir}")
    except OSError as e:
         logger.error(f"任务 {self.request.id} 无法创建任务临时文件基础目录 {base_temp_dir}: {e}")
         raise RuntimeError(f"无法创建任务临时文件基础目录: {e}") from e


    pptx_filepath = Path(pptx_filepath_str)
    output_dir_path = Path(output_dir_str)

    logger.info(f"任务 {self.request.id} 开始处理: {pptx_filepath.name}")
    start_time = time.time()
    temp_run_dir = None # 初始化临时任务目录变量

    try:
        # --- 1. 处理演示文稿 (导出图片、提取备注、生成音频) ---
        logger.info("阶段 1/2: 处理演示文稿 (导出、备注、音频)...")
        # 调用 process_presentation_for_task，传递 logger 和 config
        processed_data, temp_run_dir = process_presentation_for_task(
            pptx_filepath,
            base_temp_dir, # 传递临时目录基础路径
            voice_id,
            logger,
            config # 传递 config
        )

        if processed_data is None or temp_run_dir is None:
             # process_presentation_for_task 失败时应该抛出异常，这里是双重检查
             raise RuntimeError("演示文稿处理失败或返回无效数据。请检查详细日志。")

        # 更新任务状态 (可选，如果前端需要更细粒度的状态)
        # self.update_state(state='PROCESSING', meta={'stage': 'synthesizing_video', 'progress': 50})

        # --- 2. 合成视频 (拼接、生成字幕、添加字幕) ---
        logger.info("阶段 2/2: 合成视频 (拼接、字幕)...")
        # 生成最终视频文件名，包含任务ID的一部分，避免冲突
        # 确保安全文件名，但这里 pptx_filepath.stem 已经是 secure_filename 处理过的
        final_video_filename = f"{pptx_filepath.stem}_{self.request.id[:8]}.mp4"
        final_video_path = output_dir_path / final_video_filename
        # 确保最终输出目录存在且有权限
        try:
             output_dir_path.mkdir(parents=True, exist_ok=True)
             logger.debug(f"确保最终输出目录存在: {output_dir_path}")
        except OSError as e:
             logger.error(f"无法创建最终输出目录 {output_dir_path}: {e}")
             raise RuntimeError(f"无法创建最终输出目录: {e}") from e


        # 调用 synthesize_video_for_task，传递 logger 和 config
        synthesis_success = synthesize_video_for_task(
            processed_data,
            temp_run_dir, # 传递任务的临时目录
            final_video_path,
            logger,
            config # 传递 config
        )

        if not synthesis_success:
            # synthesize_video_for_task 失败时会返回 False，详细错误已在内部记录
            raise RuntimeError("视频合成失败。请检查详细日志。")

        # --- 任务成功完成 ---
        end_time = time.time()
        duration = end_time - start_time
        logger.info(f"任务 {self.request.id} 成功完成！输出文件: {final_video_path.resolve()}")
        logger.info(f"总耗时: {duration:.2f} 秒")

        # 返回最终视频文件相对于 Web 服务基础下载目录的路径
        # 假定 config.ini 中配置的 base_output_dir 是 Web 服务静态文件或下载服务的根目录
        base_output_dir_config = Path(config.get('General', 'base_output_dir', fallback='./output'))
        try:
             # 计算相对路径
             relative_output_path = final_video_path.relative_to(base_output_dir_config)
             # 返回相对路径字符串
             return str(relative_output_path)
        except ValueError:
             # 如果最终输出路径不在 config 中配置的基础输出目录 下，返回完整路径或文件名
             logger.warning(f"最终输出路径 '{final_video_path}' 不在配置的基础输出目录 '{base_output_dir_config}' 下，返回完整路径。")
             return str(final_video_path.resolve())


    except Exception as e:
        # --- 任务处理失败 ---
        error_msg = f"任务 {self.request.id} 处理失败: {e}"
        logger.error(error_msg, exc_info=True) # 记录完整错误信息和堆栈跟踪

        # 标记任务失败
        # Celery 默认会标记任务失败，这里可以通过 update_state 添加额外信息
        self.update_state(state='FAILURE', meta={'error': str(e)})

        # 重新抛出异常，让 Celery 记录到结果后端
        raise e # 抛出异常非常重要，否则 Celery 会认为任务成功


    finally:
        # --- 清理临时文件 ---
        cleanup_temp = config.getboolean('General', 'cleanup_temp_dir', fallback=True)
        # 只有当 temp_run_dir 变量在 try 块中被成功赋值后才尝试清理
        if cleanup_temp and temp_run_dir and temp_run_dir.exists():
            logger.info(f"任务 {self.request.id} 正在清理临时目录: {temp_run_dir}")
            try:
                shutil.rmtree(temp_run_dir)
                logger.info("临时目录已清理。")
            except Exception as clean_e:
                logger.error(f"清理临时目录 {temp_run_dir} 失败: {clean_e}")
        elif temp_run_dir and temp_run_dir.exists():
             logger.info(f"任务 {self.request.id} 临时文件保留于: {temp_run_dir} (cleanup_temp = False)")


# --- 可选：在 worker 启动时执行一些初始化操作 ---
# 例如，检查依赖工具是否存在，只在 worker 进程中运行一次
@signals.worker_init.connect
def worker_init(**kwargs):
    # logger 是 Celery Worker 的根日志记录器
    logger = logging.getLogger('celery')
    logger.info("Celery worker 正在初始化...")

    # 在 worker 启动时加载一次配置 (也可以在每个任务中加载)
    # 这个配置对象不能直接传递给任务，但可以在这里做一些基于配置的初始化检查
    config = configparser.ConfigParser()
    config_path = Path(__file__).parent.parent / 'config.ini'
    if config_path.exists():
        try:
            config.read(config_path, encoding='utf-8')
            logger.info("Worker 初始化：成功加载配置。")
        except Exception as e:
            logger.error(f"Worker 初始化：加载配置 {config_path} 失败: {e}")
            # 配置加载失败不是致命错误，但可能导致后续任务失败

    # 检查关键外部依赖是否存在
    logger.info("Worker 初始化：检查外部依赖工具...")
    dependencies_ok = True
    tools_to_check = ["ffmpeg", "ffprobe", "soffice"] # 需要检查的工具列表

    # 使用 utils 中的 get_tool_path 检查工具是否存在
    # 需要一个临时的 configparser 实例传递给 get_tool_path
    temp_config_for_check = configparser.ConfigParser()
    if config_path.exists(): temp_config_for_check.read(config_path, encoding='utf-8')

    for tool in tools_to_check:
        try:
             if get_tool_path(tool, logger, temp_config_for_check) is None:
                 logger.error(f"Worker 初始化：外部工具 '{tool}' 未找到。依赖于 '{tool}' 的任务可能会失败。")
                 dependencies_ok = False
        except Exception as e:
             logger.error(f"Worker 初始化：检查工具 '{tool}' 时发生错误: {e}", exc_info=True)
             dependencies_ok = False

    # 检查 python 库是否已导入 (这些应该在依赖安装阶段就解决)
    if not ('process_presentation_for_task' in globals() or 'process_presentation_for_task' in sys.modules):
         logger.error("Worker 初始化：Python 核心库 'process_presentation_for_task' 未导入。任务将失败。")
         dependencies_ok = False
    if not ('synthesize_video_for_task' in globals() or 'synthesize_video_for_task' in sys.modules):
         logger.error("Worker 初始化：Python 核心库 'synthesize_video_for_task' 未导入。任务将失败。")
         dependencies_ok = False
    if not ('get_available_tts_voices_core' in globals() or 'get_available_tts_voices_core' in sys.modules):
         logger.error("Worker 初始化：Python 核心库 'get_available_tts_voices_core' 未导入。")
         dependencies_ok = False # 这不阻止任务运行，但阻止获取语音列表

    # 检查 stable-ts 和 Pillow 是否成功导入 (它们的导入可能在 core_logic 文件中)
    try:
        import stable_whisper # 在这里尝试导入一次
        logger.info("Worker 初始化：Python 库 'stable-ts' (stable_whisper) 导入成功。")
    except ImportError:
        logger.error("Worker 初始化：Python 库 'stable-ts' 未导入。依赖于它的任务将失败。")
        dependencies_ok = False
    try:
        from PIL import Image # 在这里尝试导入一次
        logger.info("Worker 初始化：Python 库 'Pillow' 导入成功。")
    except ImportError:
         logger.error("Worker 初始化：Python 库 'Pillow' 未导入。")
         dependencies_ok = False

    # 检查 opencc (可选依赖)
    try:
        import opencc
        logger.info("Worker 初始化：Python 库 'opencc-python-reimplemented' 导入成功。")
    except ImportError:
         logger.warning("Worker 初始化：Python 库 'opencc-python-reimplemented' 未导入。繁简转换将跳过。")


    if dependencies_ok:
        logger.info("Worker 初始化：核心外部依赖和 Python 库检查通过。Worker 就绪。")
    else:
        logger.error("Worker 初始化：部分外部依赖或 Python 库检查未通过。请确保所有必需的软件和库已安装。")


# --- 获取可用语音列表的函数 ---
# 这个函数通常由 Web 前端调用，需要从 tasks 模块导入到 app.py
def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    """
    获取可用 TTS 语音列表。

    Args:
        logger: 日志记录器实例。 (Web 应用调用时需要传递 logger)

    Returns:
        语音信息字典列表。
    """
    # 直接调用 core_logic 中的函数
    # 注意：这个函数不应该依赖 Celery 运行时或任务上下文
    return get_available_tts_voices_core(logger)