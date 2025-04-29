# core_logic/tts_manager_edge.py
import logging
import asyncio # Edge TTS 是异步库
import edge_tts # Edge TTS 库
import tempfile # 用于创建临时文件
from pathlib import Path # 路径操作
import os # 操作系统交互
import time # 时间相关，用于重试延迟
# 导入 Edge TTS 库可能抛出的异常
from edge_tts.exceptions import NoAudioReceived, EdgeTTSException

# 导入 utils 模块，获取工具函数
# 注意：这里的导入使用了相对导入，因为 tts_manager_edge.py 在 core_logic 包内
try:
    from .utils import get_audio_duration # 导入获取音频时长函数
    # from .utils import get_tool_path # 如果 Edge TTS 依赖外部工具，需要从 utils 导入
except ImportError as e:
    # 如果 utils 导入失败，记录错误
    logging.error(f"FATAL ERROR: 无法导入 core_logic.utils 模块: {e}")
    # 这个模块是必需的，如果导入失败，下面的函数会无法正常工作
    # 可以选择在这里抛出异常，让 worker 启动失败
    # raise ImportError(f"无法导入 core_logic.utils 模块: {e}") from e


# --- 精选的 Edge TTS 语音列表 ---
# 格式: 'Voice ID': {'name': '显示名称', 'lang': '语言代码', 'gender': '性别'}
# Voice ID 可以通过命令 `edge-tts --list-voices` 查看或通过库的 list_voices 方法获取
# 这里选择一些常见且质量较好的中英文语音，用于界面展示和默认选择
KNOWN_EDGE_VOICES = {
    # --- 中文 (普通话) ---
    "zh-CN-XiaoxiaoNeural": {"name": "晓晓 (女声, 推荐)", "lang": "zh-CN", "gender": "Female"},
    "zh-CN-YunxiNeural": {"name": "云希 (男声, 推荐)", "lang": "zh-CN", "gender": "Male"},
    "zh-CN-YunjianNeural": {"name": "云健 (男声, 沉稳)", "lang": "zh-CN", "gender": "Male"},
    "zh-CN-XiaoyiNeural": {"name": "晓伊 (女声, 温柔)", "lang": "zh-CN", "gender": "Female"},
    "zh-CN-liaoning-XiaobeiNeural": {"name": "辽宁小北 (女声, 东北)", "lang": "zh-CN-liaoning", "gender": "Female"},
    "zh-CN-shaanxi-XiaoniNeural": {"name": "陕西小妮 (女声, 陕西)", "lang": "zh-CN-shaanxi", "gender": "Female"},
    # --- 英文 (美国) ---
    "en-US-JennyNeural": {"name": "Jenny (女声, 推荐)", "lang": "en-US", "gender": "Female"},
    "en-US-GuyNeural": {"name": "Guy (男声, 推荐)", "lang": "en-US", "gender": "Male"},
    "en-US-AriaNeural": {"name": "Aria (女声)", "lang": "en-US", "gender": "Female"},
    "en-US-DavisNeural": {"name": "Davis (男声)", "lang": "en-US", "gender": "Male"},
    "en-US-SaraNeural": {"name": "Sara (女声, 清晰)", "lang": "en-US", "gender": "Female"},
    "en-US-ChristopherNeural": {"name": "Christopher (男声, 成熟)", "lang": "en-US", "gender": "Male"},
    # --- 英文 (英国) ---
    "en-GB-LibbyNeural": {"name": "Libby (女声, UK)", "lang": "en-GB", "gender": "Female"},
    "en-GB-RyanNeural": {"name": "Ryan (男声, UK)", "lang": "en-GB", "gender": "Male"},
    "en-GB-SoniaNeural": {"name": "Sonia (女声, UK)", "lang": "en-GB", "gender": "Female"},
    # --- 英文 (澳大利亚) ---
    "en-AU-NatashaNeural": {"name": "Natasha (女声, AU)", "lang": "en-AU", "gender": "Female"},
    "en-AU-WilliamNeural": {"name": "William (男声, AU)", "lang": "en-AU", "gender": "Male"},
    # 可以根据需要添加更多语音ID
}

# --- 异步执行帮助函数 (在 Celery Worker 中运行异步代码) ---
# 警告：在 Celery Worker 的默认进程池 (prefork) 中直接运行 asyncio 事件循环可能会有问题，因为它不是线程安全的。
# 更好的方式是在 Celery Worker 配置中使用 eventlet 或 gevent 进程池，或者使用专门的异步任务库。
# 此函数用于在同步代码中（如 Celery Worker 的同步任务）安全地运行异步函数。
# 它为每个调用创建一个新的事件循环并在其中运行异步函数。
def run_async_in_sync(async_func):
    """
    在同步函数中运行给定的异步函数。

    Args:
        async_func: 需要运行的异步函数（协程对象）。

    Returns:
        异步函数的返回值。
    """
    loop = None
    is_new_loop = False
    try:
        # 尝试获取当前线程的事件循环。如果线程还没有循环，会抛出 RuntimeError。
        loop = asyncio.get_event_loop()
        if loop.is_running():
             # 如果循环正在运行，直接在其中创建任务并运行直到完成
             # 这在 gevent/eventlet worker 中可行，但在同步 worker 中可能导致问题
             # 明确创建一个新的循环更安全，避免干扰其他地方的潜在异步代码
             raise RuntimeError("Existing loop is running, creating new one.")

    except RuntimeError: # 通常意味着没有当前事件循环，需要创建一个新的
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop) # 设置新循环为当前线程的事件循环
        is_new_loop = True

    # 如果上面获取到或创建了循环，运行异步函数
    try:
        # 使用 loop.run_until_complete() 运行协程直到完成
        result = loop.run_until_complete(async_func)
        return result
    finally:
        # 清理：如果创建了新的事件循环，需要关闭它
        if is_new_loop and loop is not None and not loop.is_closed():
            try:
                # asyncio.run() 或 run_until_complete 在 Python 3.7+ 中应该会处理清理，
                # 但手动关闭确保资源释放
                loop.close()
            except Exception:
                # 关闭时可能出错，忽略
                pass
            # 恢复事件循环策略到默认，以免影响后续操作
            asyncio.set_event_loop_policy(None)


# --- Edge TTS 异步合成函数 ---
async def _synthesize_edge_audio_async(
    voice_id: str,
    text: str,
    output_path: Path,
    rate_str: str = "+0%",
    logger: logging.Logger = logging.getLogger(__name__) # 接收 logger 并设置默认值
):
    """
    内部异步函数：使用 edge-tts 库合成语音并保存到文件。

    Args:
        voice_id: 要使用的语音 ID。
        text: 要合成的文本。
        output_path: 输出音频文件路径 (Path 对象)。
        rate_str: 语速字符串 (例如 "+0%", "+10%", "-5%")。
        logger: 日志记录器实例。

    Returns:
        bool: 合成并保存成功返回 True，否则返回 False。
    """
    logger.debug(f"开始异步合成: Voice='{voice_id}', Rate='{rate_str}', Text='{text[:50]}...'") # 记录部分文本
    try:
        # 实例化 Edge TTS Communicate 对象
        # pitch 参数在这里被移除，因为我们的需求中不再支持通过参数控制音调
        communicate = edge_tts.Communicate(text, voice_id, rate=rate_str)

        # 使用 await communicate.save() 执行异步操作并保存到文件
        await communicate.save(str(output_path))

        logger.debug(f"异步合成完成，已保存到: {output_path.name}")
        return True # 成功

    except NoAudioReceived as e:
        # 如果服务器没有返回音频数据
        logger.error(f"Edge TTS 错误 (NoAudioReceived): Voice='{voice_id}', Rate='{rate_str}'. {e}")
        return False # 失败
    except EdgeTTSException as e:
        # 捕获 Edge TTS 库特有的其他异常
        logger.error(f"Edge TTS 库错误: Voice='{voice_id}', Rate='{rate_str}'. Error: {e}")
        return False # 失败
    except Exception as e:
        # 捕获其他可能的异常（如网络错误 aiohttp.ClientError 等）
        logger.error(f"异步合成时发生意外错误: Voice='{voice_id}', Rate='{rate_str}'. Error: {e}", exc_info=True)
        return False # 失败


# --- TTS 生成片段函数 (由 ppt_processor 调用) ---
def generate_audio_segment(
    voice_id: str,
    text: str,
    output_path: Path,
    rate: int, # 语速百分比
    logger: logging.Logger, # 接收 logger
    max_retries: int = 1, # 最大重试次数
    retry_delay: float = 1.5 # 重试间隔（秒）
) -> bool:
    """
    为演讲稿的一个片段生成音频文件 (MP3)，包含重试逻辑。
    此函数是同步的，它在内部调用异步合成函数。

    Args:
        voice_id: 要使用的语音 ID。
        text: 要转换的文本片段。
        output_path: 要保存的音频文件路径 (Path 对象, e.g., segment_1.mp3)。
        rate: 语速百分比 (100 表示正常)。
        logger: 日志记录器实例。
        max_retries: 最大重试次数。
        retry_delay: 重试间隔时间（秒）。

    Returns:
        True 如果成功生成音频文件, False 如果失败。
    """
    logger.debug(f"请求 Edge TTS 片段音频: Voice='{voice_id}', Rate={rate}%, Output='{output_path.name}', Text='{text[:50]}...'") # 记录部分文本
    if voice_id not in KNOWN_EDGE_VOICES:
        logger.error(f"无效的语音 ID: '{voice_id}'")
        return False
    if not text or text.isspace():
        logger.warning(f"文本片段为空，跳过 TTS: {output_path.name}")
        # 对于空文本，不生成文件，返回 True 表示“跳过生成成功”
        # 或者返回 False 让调用者知道没有文件生成
        # 这里返回 False，让调用者（ppt_processor）知道没有音频文件
        return False

    # 将百分比转换为 Edge TTS 需要的格式 (+x% 或 -x%)
    rate_str = f"{rate-100:+d}%"

    # 确保父目录存在
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
         logger.error(f"无法创建音频输出目录 {output_path.parent}: {e}")
         return False

    for attempt in range(max_retries + 1):
        success = False # 标记本次尝试是否成功生成文件

        try:
            # --- 调用异步合成函数并在同步环境中运行 ---
            # 创建异步任务 (协程对象)
            async_task_coroutine = _synthesize_edge_audio_async( # <--- 确保调用结果被赋值！
                voice_id,
                text,
                output_path,
                rate_str,
                logger # 传递 logger
            )
            # 在同步环境中运行异步任务
            synthesis_result = run_async_in_sync(async_task_coroutine) # <--- 使用正确的变量名

            # synthesis_result 是 _synthesize_edge_audio_async 的返回值 (True 或 False)
            if synthesis_result:
                # 检查文件是否存在且不为空，作为最终确认
                if output_path.exists() and output_path.stat().st_size > 100:
                    logger.info(f"  尝试 {attempt+1}/{max_retries+1}: Edge TTS 片段音频生成成功: {output_path.name}")
                    success = True # 本次尝试成功
                    return True # 生成成功，直接返回 True

                else:
                    # 异步函数返回 True，但文件无效，这是一种异常情况
                    logger.warning(f"  尝试 {attempt+1}/{max_retries+1}: 异步合成返回成功，但文件为空或过小: {output_path.name}")
                    if output_path.exists(): output_path.unlink(missing_ok=True) # 删除无效文件
                    success = False # 本次尝试失败
            else:
                 # 异步合成函数返回 False，表示 Edge TTS 发生了错误
                 logger.warning(f"  尝试 {attempt+1}/{max_retries+1}: 异步合成函数返回失败 for {output_path.name}")
                 success = False # 本次尝试失败


        except Exception as e: # 捕获 run_async_in_sync 或其他意外错误
            logger.error(f"  尝试 {attempt+1}/{max_retries+1}: 生成 Edge TTS 片段时发生意外错误: {e}", exc_info=True)
            success = False # 本次尝试失败

        # --- 重试逻辑判断 ---
        if not success and attempt < max_retries:
            logger.info(f"将在 {retry_delay} 秒后重试 ({attempt+2}/{max_retries+1})...")
            time.sleep(retry_delay) # 同步等待重试
        elif not success: # 达到最大重试次数仍然失败
             logger.error(f"达到最大重试次数 ({max_retries} 次)，生成片段 '{output_path.name}' 最终失败。")
             if output_path.exists(): output_path.unlink(missing_ok=True) # 清理可能残留的空文件
             return False # 最终失败，返回 False

    # 理论上代码不会执行到这里，因为循环内要么成功返回 True，要么重试耗尽返回 False
    # 这是一个逻辑上的回退，表明流程未按预期结束
    logger.error(f"代码逻辑错误：Edge TTS 片段生成函数循环结束但未确定状态 for {output_path.name}")
    return False # 返回 False 表示函数未成功完成


# --- 获取可用语音列表的函数 (由 app.py 或 tasks.py 调用) ---
def get_available_voices(logger: logging.Logger) -> list[dict]:
    """
    获取预定义的 Edge TTS 语音列表。

    Args:
        logger: 日志记录器实例。

    Returns:
        语音信息字典列表。
    """
    logger.info("获取预定义的 Edge TTS 语音列表。")
    voice_list = []
    for voice_id, details in KNOWN_EDGE_VOICES.items():
        voice_info = details.copy()
        voice_info['id'] = voice_id
        voice_list.append(voice_info)
    # 按显示名称排序
    voice_list.sort(key=lambda x: x.get('name', ''))

    if not voice_list:
        logger.warning("预定义的 Edge TTS 语音列表为空，请检查 KNOWN_EDGE_VOICES 字典。")

    return voice_list

# --- generate_preview_audio 函数 ---
# 这个函数主要是给 GUI 或测试用的，在服务端的核心转换任务中不直接使用
# 但如果 Flask 前端提供了试听功能，它就会被 Flask 调用
def generate_preview_audio(voice_id: str, logger: logging.Logger, text: str | None = None) -> str | None:
    """
    (主要用于测试/GUI) 使用指定的 Edge TTS voice_id 生成预览音频 (MP3)。
    此函数是同步的，内部调用异步合成。

    Args:
        voice_id: 要使用的语音 ID。
        logger: 日志记录器实例。
        text: (可选) 要转换为语音的示例文本。如果为 None，会根据语音语言选择默认文本。

    Returns:
        成功生成的临时音频文件 (mp3) 的绝对路径。如果失败则返回 None。
        注意：调用者负责在使用后删除此临时文件。
    """
    logger.info(f"请求 Edge TTS 预览: Voice ID='{voice_id}'")
    if voice_id not in KNOWN_EDGE_VOICES:
        logger.error(f"预览错误：无效的语音 ID: '{voice_id}'")
        return None

    if text is None:
        lang_prefix = KNOWN_EDGE_VOICES[voice_id].get('lang', 'en').split('-')[0].lower()
        text = "你好，这是一个使用微软 Edge 语音合成的试听示例。" if lang_prefix == 'zh' else "Hello, this is an audio preview using Microsoft Edge speech synthesis."

    temp_file_path = None
    try:
        # 创建临时文件来保存音频
        # mkstemp 返回一个文件描述符和一个路径字符串
        fd, temp_file_path_str = tempfile.mkstemp(suffix=".mp3", prefix="tts_preview_")
        os.close(fd) # 关闭文件描述符，以便 edge-tts 可以通过路径打开和写入文件
        temp_file_path = Path(temp_file_path_str)
        logger.debug(f"创建临时预览文件: {temp_file_path}")

        # --- 调用异步合成函数并在同步环境中运行 ---
        # 创建异步任务 (协程对象)
        async_task_coroutine = _synthesize_edge_audio_async( # <--- 确保调用结果被赋值！
            voice_id,
            text,
            temp_file_path,
            "+0%", # 预览通常使用默认速率
            logger # 传递 logger
        )
        # 在同步环境中运行异步任务
        synthesis_result = run_async_in_sync(async_task_coroutine) # <--- 使用正确的变量名
        # --- ------------- ---

        if synthesis_result:
            # 检查文件是否存在且不为空，作为最终确认
            if temp_file_path.exists() and temp_file_path.stat().st_size > 100:
                logger.info(f"Edge TTS 预览音频生成成功: {temp_file_path}")
                return str(temp_file_path.resolve()) # 返回绝对路径字符串
            else:
                logger.error("Edge TTS 未能成功生成预览音频文件或文件为空。")
                if temp_file_path.exists(): os.remove(temp_file_path) # 删除无效文件
                return None # 失败
        else:
             # 异步合成函数返回 False，表示 Edge TTS 发生了错误
             logger.error("异步合成函数返回失败 for preview.")
             if temp_file_path.exists(): os.remove(temp_file_path) # 删除可能残留的空文件
             return None # 失败


    except Exception as e:
        logger.error(f"生成 Edge TTS 预览音频时发生错误: {e}", exc_info=True)
        if temp_file_path and temp_file_path.exists():
            try: os.remove(temp_file_path) # 尝试清理残留文件
            except OSError: pass
        return None