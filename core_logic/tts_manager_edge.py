# core_logic/tts_manager_edge.py
import logging
import asyncio
import edge_tts
import tempfile
from pathlib import Path
import os
import time # 需要 time 来实现重试延迟

# 假设 utils.py 提供了 get_audio_duration
# from .utils import get_audio_duration

# --- 精选的 Edge TTS 语音列表 (保持不变) ---
KNOWN_EDGE_VOICES = {
    # ... (列表内容同前) ...
     "zh-CN-XiaoxiaoNeural": {"name": "晓晓 (女声, 推荐)", "lang": "zh-CN", "gender": "Female"},
    "zh-CN-YunxiNeural": {"name": "云希 (男声, 推荐)", "lang": "zh-CN", "gender": "Male"},
    "zh-CN-YunjianNeural": {"name": "云健 (男声, 沉稳)", "lang": "zh-CN", "gender": "Male"},
    "zh-CN-XiaoyiNeural": {"name": "晓伊 (女声, 温柔)", "lang": "zh-CN", "gender": "Female"},
    "zh-CN-liaoning-XiaobeiNeural": {"name": "辽宁小北 (女声, 东北)", "lang": "zh-CN-liaoning", "gender": "Female"}, # 地方口音示例
    "zh-CN-shaanxi-XiaoniNeural": {"name": "陕西小妮 (女声, 陕西)", "lang": "zh-CN-shaanxi", "gender": "Female"}, # 地方口音示例
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
}


# --- 异步执行帮助函数 (在 Celery 中可能需要调整) ---
def run_async_in_sync(async_func):
    """
    警告：在 Celery worker 中直接运行此函数可能会阻塞 worker！
    生产环境建议使用 Celery 的异步机制或 eventlet/gevent。
    此函数用于在非异步环境中（如测试脚本或简单回调）运行异步代码。
    """
    loop = None
    try:
        # 尝试获取现有事件循环 (在某些 Celery 配置下可能存在)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果已有循环在运行，创建新循环可能导致问题
            # 这里简单地创建新循环并在其中运行，但在复杂场景下需谨慎
            # logging.warning("检测到正在运行的事件循环，仍创建新循环执行异步任务。") # 使用 logging
            pass # 允许在已运行的循环上创建新任务（如果策略允许）
    except RuntimeError: # 通常意味着没有当前事件循环
        pass # loop is None

    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        is_new_loop = True
        # logging.debug("创建了新的事件循环来运行异步任务。")
    else:
        is_new_loop = False
        # logging.debug("在现有事件循环上运行异步任务。")


    try:
        # 运行异步任务直到完成
        result = loop.run_until_complete(async_task)
        return result
    finally:
        if is_new_loop:
            # 关闭我们创建的新循环
            try:
                loop.close()
                # logging.debug("已关闭新创建的事件循环。")
            except Exception as e:
                 # logging.error(f"关闭新事件循环时出错: {e}") # 使用 logging
                 pass
            # 重置事件循环策略，以便下次调用能正确创建
            asyncio.set_event_loop_policy(None)


# --- TTS 功能函数 ---
def get_available_voices(logger: logging.Logger) -> list[dict]:
    """返回预定义的 Edge TTS 语音列表。"""
    logger.info("获取预定义的 Edge TTS 语音列表。")
    voice_list = []
    for voice_id, details in KNOWN_EDGE_VOICES.items():
        voice_info = details.copy()
        voice_info['id'] = voice_id
        voice_list.append(voice_info)
    # 按显示名称排序
    voice_list.sort(key=lambda x: x.get('name', ''))
    return voice_list

async def _synthesize_edge_audio_async(
    voice_id: str,
    text: str,
    output_path: Path,
    rate_str: str,
    logger: logging.Logger # 接收 logger
):
    """异步执行 Edge TTS 合成并保存到文件。"""
    logger.debug(f"开始异步合成: Voice='{voice_id}', Rate='{rate_str}', Text='{text[:30]}...'")
    try:
        communicate = edge_tts.Communicate(text, voice_id, rate=rate_str)
        await communicate.save(str(output_path))
        logger.debug(f"异步合成完成，已保存到: {output_path.name}")
        return True # 返回成功状态
    except edge_tts.NoAudioReceived as e:
        logger.error(f"Edge TTS 错误 (NoAudioReceived): Voice='{voice_id}', Rate='{rate_str}'. {e}")
        return False
    except Exception as e:
        # 捕获其他可能的 edge_tts 或 aiohttp 异常
        logger.error(f"异步合成时发生错误: Voice='{voice_id}', Rate='{rate_str}'. Error: {e}", exc_info=True)
        return False


def generate_audio_segment(
    voice_id: str,
    text: str,
    output_path: Path,
    rate: int,
    logger: logging.Logger, # 接收 logger
    max_retries: int = 1, # 默认重试1次
    retry_delay: float = 1.5 # 重试间隔
) -> bool:
    """
    为演讲稿的一个片段生成音频文件 (MP3)，包含重试逻辑。

    Args:
        voice_id: 要使用的语音 ID。
        text: 要转换的文本片段。
        output_path: 要保存的音频文件路径 (Path 对象)。
        rate: 语速百分比 (100 表示正常)。
        logger: 日志记录器实例。
        max_retries: 最大重试次数。
        retry_delay: 重试间隔时间（秒）。

    Returns:
        True 如果成功生成音频文件, False 如果失败。
    """
    logger.debug(f"请求 Edge TTS 片段音频: Voice='{voice_id}', Rate={rate}%, Output='{output_path.name}', Text='{text[:30]}...'")
    if voice_id not in KNOWN_EDGE_VOICES:
        logger.error(f"无效的语音 ID: '{voice_id}'")
        return False
    if not text or text.isspace():
        logger.warning(f"文本片段为空，跳过 TTS: {output_path.name}")
        return False # 不生成文件

    # 将百分比转换为 Edge TTS 需要的格式
    rate_str = f"{rate-100:+d}%"

    # 确保父目录存在
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
         logger.error(f"无法创建音频输出目录 {output_path.parent}: {e}")
         return False

    for attempt in range(max_retries + 1):
        success = False
        try:
            # --- 运行异步合成 ---
            # 注意：这里的 run_async_in_sync 在 Celery 环境下可能阻塞 worker
            async_task = _synthesize_edge_audio_async(voice_id, text, output_path, rate_str, logger)
            synthesis_result = run_async_in_sync(async_task)
            # --- ------------- ---

            if synthesis_result:
                # 检查文件是否存在且不为空
                if output_path.exists() and output_path.stat().st_size > 100:
                    logger.info(f"  尝试 {attempt+1}/{max_retries+1}: Edge TTS 片段音频生成成功: {output_path.name}")
                    success = True
                    return True # 成功，直接返回
                else:
                    logger.warning(f"  尝试 {attempt+1}/{max_retries+1}: Edge TTS 文件生成了，但为空或过小: {output_path.name}")
                    if output_path.exists(): output_path.unlink(missing_ok=True) # 删除无效文件
                    # success 保持 False，继续判断是否重试
            else:
                 # _synthesize_edge_audio_async 内部已经记录了错误
                 logger.warning(f"  尝试 {attempt+1}/{max_retries+1}: 异步合成函数返回失败 for {output_path.name}")
                 # success 保持 False

        except Exception as e: # 捕获 run_async_in_sync 或其他意外错误
            logger.error(f"  尝试 {attempt+1}/{max_retries+1}: 生成 Edge TTS 片段时发生意外错误: {e}", exc_info=True)
            # success 保持 False

        # 如果失败且还有重试次数
        if not success and attempt < max_retries:
            logger.info(f"将在 {retry_delay} 秒后重试 ({attempt+2}/{max_retries+1})...")
            time.sleep(retry_delay) # 同步等待
        elif not success: # 达到最大重试次数仍然失败
             logger.error(f"达到最大重试次数，生成片段 '{output_path.name}' 最终失败。")
             if output_path.exists(): output_path.unlink(missing_ok=True) # 清理可能残留的空文件
             return False

    # 理论上代码不会执行到这里，因为循环内要么成功返回 True，要么重试耗尽返回 False
    logger.error(f"代码逻辑错误：循环结束但未确定状态 for {output_path.name}")
    return False

# --- generate_preview_audio 函数 ---
# 这个函数主要是给 GUI 用的，在服务端意义不大，但可以保留用于测试或未来可能的预览功能
def generate_preview_audio(voice_id: str, logger: logging.Logger, text: str | None = None) -> str | None:
    """
    (主要用于测试/GUI) 使用指定的 Edge TTS voice_id 生成预览音频 (MP3)。

    Args:
        voice_id: 要使用的语音 ID。
        logger: 日志记录器实例。
        text: (可选) 示例文本。如果为 None，会选择默认文本。

    Returns:
        成功生成的临时音频文件路径 (str)，失败则返回 None。调用者负责删除。
    """
    logger.info(f"请求 Edge TTS 预览: Voice ID='{voice_id}'")
    if voice_id not in KNOWN_EDGE_VOICES:
        logger.error(f"预览错误：无效的语音 ID: '{voice_id}'")
        return None

    if text is None:
        lang_prefix = KNOWN_EDGE_VOICES[voice_id].get('lang', 'en').split('-')[0].lower()
        text = "你好，这是微软 Edge 语音合成的试听。" if lang_prefix == 'zh' else "Hello, this is an audio preview using Microsoft Edge speech synthesis."

    temp_file_path = None
    try:
        # 创建临时文件来保存音频
        fd, temp_file_path_str = tempfile.mkstemp(suffix=".mp3", prefix="tts_preview_")
        os.close(fd) # 关闭文件描述符，让 edge-tts 可以写入
        temp_file_path = Path(temp_file_path_str)
        logger.info(f"创建临时预览文件: {temp_file_path}")

        # --- 运行异步合成 ---
        # 预览通常不需要调整速率，使用默认值 "+0%"
        async_task = _synthesize_edge_audio_async(voice_id, text, temp_file_path, "+0%", logger)
        synthesis_result = run_async_in_sync(async_task)
        # --- ------------- ---

        if synthesis_result and temp_file_path.exists() and temp_file_path.stat().st_size > 100:
            logger.info(f"Edge TTS 预览音频生成成功: {temp_file_path}")
            return str(temp_file_path.resolve())
        else:
            logger.error("Edge TTS 未能成功生成预览音频文件或文件为空。")
            if temp_file_path.exists(): os.remove(temp_file_path)
            return None

    except Exception as e:
        logger.error(f"生成 Edge TTS 预览音频时发生错误: {e}", exc_info=True)
        if temp_file_path and temp_file_path.exists():
            try: os.remove(temp_file_path)
            except OSError: pass
        return None