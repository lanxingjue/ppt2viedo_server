# core_logic/ppt_processor.py
import os
import platform
import time
import shutil
from pathlib import Path
import logging
import uuid
import configparser
import json
import subprocess
import shlex
import sys # 需要导入 sys 来处理潜在的 exit 调用 (现在应该没了，但导入无害)

# 导入 mutagen 用于 MP3 时长获取 (如果 utils 中没处理)
try:
    from mutagen import File as MutagenFile, MutagenError
    MUTAGEN_AVAILABLE = True
except ImportError:
    logging.warning("缺少 'mutagen' 库，MP3 时长可能不准。'pip install mutagen'") # 使用 logging
    MUTAGEN_AVAILABLE = False

# 导入同级模块使用相对路径
from .tts_manager_edge import generate_audio_segment, get_available_voices # 导入 tts 管理器
from .ppt_exporter_libreoffice import export_slides_with_libreoffice # 只导入 LibreOffice 导出器
from .utils import get_audio_duration # 从 utils 导入时长获取函数

# 导入 Presentation 类
try:
    from pptx import Presentation
except ImportError:
    logging.error("缺少 'python-pptx' 库！请运行 'pip install python-pptx'")
    # 在模块级别无法导入，需要处理。可以在调用此模块的 Celery 任务开始时检查。
    # 或者在这里抛出异常，让 Celery 任务失败。
    raise ImportError("缺少必需的 python-pptx 库")


def extract_speaker_notes(pptx_filepath: Path, logger: logging.Logger) -> list[str]:
    """
    从 PPTX 文件提取演讲者备注。失败时抛出异常。

    Args:
        pptx_filepath: PPTX 文件路径。
        logger: 日志记录器实例。

    Returns:
        包含每张幻灯片备注文本的列表。

    Raises:
        FileNotFoundError: 如果输入文件不存在。
        Exception: 如果解析过程中发生其他错误。
    """
    if not pptx_filepath.is_file():
        logger.error(f"输入文件不存在: {pptx_filepath}")
        raise FileNotFoundError(f"输入文件不存在: {pptx_filepath}")

    notes_list = []
    try:
        logger.info(f"开始解析演示文稿以提取备注: {pptx_filepath.name}")
        prs = Presentation(pptx_filepath)
        num_slides = len(prs.slides)
        logger.info(f"演示文稿包含 {num_slides} 张幻灯片。")

        for i, slide in enumerate(prs.slides):
            slide_num = i + 1
            note_text = "" # 默认为空字符串
            if slide.has_notes_slide:
                notes_slide = slide.notes_slide
                text_frame = notes_slide.notes_text_frame
                if text_frame and text_frame.text:
                    note_text = text_frame.text.strip()
                    logger.debug(f"  找到幻灯片 {slide_num} 的备注: '{note_text[:50]}...'")
            else:
                logger.debug(f"  幻灯片 {slide_num} 没有备注。")
            notes_list.append(note_text)

        logger.info(f"成功提取了 {len(notes_list)} 条备注信息。")
        return notes_list

    except Exception as e:
        logger.error(f"解析 PPTX 文件以提取备注时出错: {e}", exc_info=True)
        raise Exception(f"解析备注失败: {e}") from e # 重新抛出，包含原始异常


def process_presentation_for_task(
    pptx_filepath: Path,
    temp_base_dir: Path,
    voice_id: str,
    logger: logging.Logger, # 接收 logger 实例
    config: configparser.ConfigParser # 接收配置对象
) -> tuple[list[dict], Path]:
    """
    处理演示文稿的核心后台任务逻辑。
    包括：导出幻灯片图片、提取备注、生成音频片段。
    失败时应抛出异常，成功时返回处理后的数据和临时目录路径。

    Args:
        pptx_filepath: 输入的 PPTX 文件路径。
        temp_base_dir: 用于创建本次任务临时目录的基础路径。
        voice_id: 要使用的 Edge TTS 语音 ID。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        一个包含 (处理数据列表, 临时目录路径) 的元组。

    Raises:
        FileNotFoundError: 输入文件未找到。
        ValueError: 输入参数无效（如 voice_id 为空）。
        OSError: 创建临时目录失败。
        RuntimeError: 处理步骤中发生无法恢复的错误。
        Exception: 其他意外错误。
    """
    logger.info(f"开始为任务处理演示文稿: {pptx_filepath.name}")

    if not pptx_filepath.is_file():
        raise FileNotFoundError(f"输入 PPTX 文件不存在: {pptx_filepath}")
    if not voice_id:
        raise ValueError("必须提供有效的 voice_id!")
    if not temp_base_dir or not isinstance(temp_base_dir, Path):
        raise ValueError("必须提供有效的临时目录基础路径 (temp_base_dir)")

    # --- 创建本次任务的独立临时目录 ---
    run_id = uuid.uuid4().hex[:8]
    # 使用更健壮的方式创建临时目录名，避免特殊字符问题
    safe_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in pptx_filepath.stem)
    temp_run_dir = temp_base_dir / f"task_{run_id}_{safe_stem}"
    temp_image_dir = temp_run_dir / "images"
    temp_audio_dir = temp_run_dir / "audio"
    try:
        temp_run_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"创建任务临时目录: {temp_run_dir}")
    except OSError as e:
        raise OSError(f"无法创建临时目录 {temp_run_dir}: {e}") from e

    # --- 步骤 1: 导出幻灯片图片 (只使用 LibreOffice) ---
    logger.info("--- 步骤 1: 使用 LibreOffice 导出幻灯片图片 ---")
    image_paths = None
    try:
        # 将 logger 和 config 传递给导出函数
        image_paths = export_slides_with_libreoffice(pptx_filepath, temp_image_dir, logger, config)
        if not image_paths:
            # 导出函数内部应该记录了错误，这里直接抛出异常
            raise RuntimeError("LibreOffice 导出幻灯片图片失败或未返回任何路径。")
        logger.info(f"成功导出 {len(image_paths)} 张图片。")
    except Exception as e:
        logger.error(f"导出幻灯片步骤发生错误: {e}", exc_info=True)
        raise RuntimeError(f"幻灯片导出失败: {e}") from e # 重新抛出

    # --- 步骤 2: 提取备注 ---
    logger.info("--- 步骤 2: 提取演讲者备注 ---")
    try:
        notes_list = extract_speaker_notes(pptx_filepath, logger)
        # extract_speaker_notes 失败时会抛出异常
    except Exception as e:
        # logger.error(f"提取备注时出错: {e}", exc_info=True) # 内部函数已记录
        raise RuntimeError(f"提取备注失败: {e}") from e

    # --- 步骤 3: 对齐图片和备注 ---
    num_images = len(image_paths)
    num_notes = len(notes_list)
    if num_images != num_notes:
        logger.warning(f"图片数({num_images})与备注数({num_notes})不匹配，将按较小数处理。")
        min_count = min(num_images, num_notes)
        if min_count == 0:
             raise ValueError("图片或备注数量为零，无法继续处理。")
        image_paths = image_paths[:min_count]
        notes_list = notes_list[:min_count]
    elif num_images == 0: # 如果原始数量就是 0
        raise ValueError("未找到任何有效的幻灯片图片。")

    logger.info(f"将处理 {len(image_paths)} 张对齐的幻灯片/备注。")


    # --- 步骤 4: 生成音频片段 ---
    logger.info(f"--- 步骤 4: 生成音频片段 (Edge TTS, Voice: {voice_id}) ---")
    # 确保音频目录存在
    try:
        temp_audio_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
         raise OSError(f"无法创建音频临时目录 {temp_audio_dir}: {e}") from e

    audio_results = []
    rate_percent = config.getint('Audio', 'tts_rate_percent', fallback=100)
    tts_retries = config.getint('Audio', 'tts_retries', fallback=1)
    tts_retry_delay = config.getfloat('Audio', 'tts_retry_delay', fallback=1.5)
    total_valid_duration = 0.0
    segment_processing_errors = 0 # 记录处理失败的片段数

    for i in range(len(notes_list)):
        segment_num = i + 1
        text = notes_list[i]
        # 使用 pathlib 构建路径
        audio_filepath = temp_audio_dir / f"segment_{segment_num}.mp3"
        audio_path_str = None
        duration_sec = 0.0 # 默认时长为 0

        if text and not text.isspace():
            logger.info(f"  生成片段 {segment_num} 音频 (共 {len(notes_list)} 个)...")
            # 调用 tts_manager 中的函数
            success = generate_audio_segment(
                voice_id,
                text,
                audio_filepath,
                rate=rate_percent,
                logger=logger, # 传递 logger
                max_retries=tts_retries,
                retry_delay=tts_retry_delay
            )

            if success:
                # 音频生成成功，尝试获取时长
                duration_sec_raw = get_audio_duration(audio_filepath, logger, config)
                if duration_sec_raw is not None and duration_sec_raw > 0.01:
                    duration_sec = duration_sec_raw
                    audio_path_str = str(audio_filepath.resolve())
                    total_valid_duration += duration_sec
                    logger.info(f"    片段 {segment_num} 处理成功, 时长: {duration_sec:.3f}s")
                elif duration_sec_raw is not None: # 时长为 0 或过短
                    audio_path_str = str(audio_filepath.resolve()) # 文件存在
                    logger.warning(f"    片段 {segment_num} 音频时长无效或过短 ({duration_sec_raw:.3f}s)，时长记为 0。")
                    duration_sec = 0.0
                    segment_processing_errors += 1
                else: # 获取时长失败
                    audio_path_str = str(audio_filepath.resolve()) # 文件存在
                    logger.error(f"    无法获取片段 {segment_num} ({audio_filepath.name}) 的有效时长！时长记为 0。")
                    duration_sec = 0.0
                    segment_processing_errors += 1
            else: # TTS 生成失败
                logger.error(f"    片段 {segment_num} TTS 生成失败。")
                duration_sec = 0.0
                segment_processing_errors += 1
        else:
            logger.info(f"  片段 {segment_num}: 文本为空，跳过音频生成，时长为 0。")
            duration_sec = 0.0

        audio_results.append((audio_path_str, duration_sec))

    logger.info(f"音频生成过程完成。有效音频总时长约: {total_valid_duration:.2f} 秒。")
    if segment_processing_errors > 0:
        logger.warning(f"{segment_processing_errors} 个音频片段在生成或时长获取中遇到问题。")


    # --- 步骤 5: 组合结果 ---
    logger.info("--- 步骤 5: 整理处理结果 ---")
    final_data = []
    for i in range(len(notes_list)): # 使用对齐后的 notes_list 长度
        image_path_str = image_paths[i]
        audio_path, duration = audio_results[i]

        # 再次确认图片路径有效
        if not Path(image_path_str).is_file():
            logger.warning(f"最终整理时发现幻灯片 {i+1} 的图片路径无效: {image_path_str}，跳过此幻灯片。")
            continue

        slide_data = {
            'slide_number': i + 1,
            'image_path': image_path_str,
            'notes': notes_list[i] or "",
            'audio_path': audio_path, # 可能为 None
            'audio_duration': duration  # 保证是 float (0.0 或有效时长)
        }
        final_data.append(slide_data)
        logger.debug(f"  整理数据: Slide {i+1}, Img: {Path(image_path_str).name}, Audio: {Path(audio_path).name if audio_path else 'N/A'}, Dur: {duration:.3f}s")

    if not final_data:
        raise ValueError("未能整理出任何有效的幻灯片数据进行后续处理。")

    logger.info(f"成功整理了 {len(final_data)} 张幻灯片的数据。")
    return final_data, temp_run_dir # 返回处理好的数据和临时目录路径