# core_logic/ppt_exporter_libreoffice.py
import os
import platform
import subprocess
import logging
import shutil
from pathlib import Path
import tempfile
import configparser
import shlex # 确保导入 shlex

# 导入共享工具函数
from .utils import get_tool_path, get_poppler_path

# 导入 pdf2image
try:
    # 注意：这里只导入需要用到的函数
    from pdf2image import convert_from_path, pdfinfo_from_path # 导入 pdfinfo_from_path 用于获取页数
    PDF2IMAGE_AVAILABLE = True
    # 导入 pdf2image 可能抛出的异常
    from pdf2image.exceptions import PDFInfoNotInstalledError, PDFSyntaxError, PDFPageCountError, PDFPopplerTimeoutError
except ImportError:
    logging.error("FATAL ERROR: 缺少 'pdf2image' 库。请运行 'pip install pdf2image'。")
    PDF2IMAGE_AVAILABLE = False
except Exception as e:
    # 捕获 pdf2image 导入时可能发生的其他错误
    logging.error(f"FATAL ERROR: 导入 'pdf2image' 库时发生意外错误: {e}", exc_info=True)
    PDF2IMAGE_AVAILABLE = False


# --- 定义任务阶段常量 (与 tasks.py 保持一致) ---
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_PPT_IMAGES = 'Exporting Slides' # 当前模块主要负责的阶段
STAGE_EXTRACT_NOTES = 'Extracting Notes'
STAGE_GENERATE_AUDIO = 'Generating Audio'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video'
STAGE_VIDEO_SEGMENTS = 'Creating Video Segments'
STAGE_VIDEO_CONCAT = 'Concatenating Video'
STAGE_GENERATE_SUBTITLES = 'Generating Subtitles (ASR)'
STAGE_ADD_SUBTITLES = 'Adding Subtitles'
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'


def export_slides_with_libreoffice(
    pptx_filepath: Path,
    output_dir: Path,
    logger: logging.Logger,
    config: configparser.ConfigParser,
    task_instance # <--- 增加这个参数来接收任务实例
) -> list[str] | None:
    """
    使用 LibreOffice 将 PPTX 转换为 PDF，然后使用 pdf2image 将 PDF 转换为 PNG 图片。
    失败时返回 None。可以在内部发送状态更新。

    Args:
        pptx_filepath: 输入的 PPTX 文件的 Path 对象。
        output_dir: 保存导出 PNG 图片的目标目录的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。
        task_instance: Celery 任务实例 (用于发送状态更新)。

    Returns:
        一个包含所有成功导出的图片文件绝对路径的列表 (list[str])。
        如果发生错误，则返回 None。
    """
    task_id = task_instance.request.id
    logger.debug(f"任务 {task_id} 调用 export_slides_with_libreoffice")
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 0, 'status': 'Starting slide export with LibreOffice'})

    if not PDF2IMAGE_AVAILABLE:
        logger.error("pdf2image 库不可用，无法进行 PDF 到图片的转换。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: pdf2image not available'})
        return None

    # 使用工具函数获取 LibreOffice 路径
    libreoffice_path = get_tool_path("soffice", logger, config)
    if libreoffice_path is None:
        logger.error("LibreOffice (soffice) 未找到。请检查安装和配置。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: soffice not found'})
        return None

    # 使用工具函数获取 Poppler 路径 (pdf2image 需要)
    poppler_path_for_pdf2image = get_poppler_path(logger, config) # 这个函数可能返回 None

    # 1. 检查输入文件
    if not pptx_filepath.is_file():
        logger.error(f"输入文件不存在: {pptx_filepath}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: Input file not found'})
        return None

    # 2. 确保输出目录存在
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"确保输出目录存在: {output_dir}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 2, 'status': 'Output directory checked'})
    except OSError as e:
        logger.error(f"创建或访问输出目录失败: {output_dir} - {e}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': f'Error: Cannot access output directory ({type(e).__name__})'})
        return None

    # 3. 创建临时目录存放中间 PDF 文件
    # 使用 with 语句确保临时目录会被清理
    temp_pdf_dir = None # 初始化变量
    try:
        with tempfile.TemporaryDirectory(prefix="lo_pdf_", dir=str(Path(config.get('General', 'base_temp_dir', fallback='/tmp')).resolve())) as temp_pdf_dir_str: # 尝试在配置的临时目录下创建
            temp_pdf_dir = Path(temp_pdf_dir_str)
            pdf_output_path = temp_pdf_dir / f"{pptx_filepath.stem}.pdf"
            logger.debug(f"创建临时 PDF 目录: {temp_pdf_dir}")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 5, 'status': 'Temp PDF directory created'})

            # 4. 调用 LibreOffice 将 PPTX 转换为 PDF
            cmd_convert_to_pdf = [
                libreoffice_path,
                "--headless",           # 无头模式
                "--invisible",          # 尝试添加 invisible 标志
                "--nologo",             # 不显示 logo
                "--nolockcheck",        # 禁用文件锁定检查
                "--norestore",          # 禁用恢复功能
                "--convert-to", "pdf:writer_pdf_Export", # 更明确的 PDF 导出 filter
                "--outdir", str(temp_pdf_dir.resolve()),
                str(pptx_filepath.resolve())
            ]
            logger.debug(f"执行 LibreOffice 命令: {shlex.join(cmd_convert_to_pdf)}")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 10, 'status': 'Running LibreOffice conversion'})
            try:
                timeout_seconds = config.getint('General', 'libreoffice_timeout', fallback=180)
                logger.debug(f"LibreOffice 超时设置为: {timeout_seconds} 秒")
                result_pdf = subprocess.run(
                    cmd_convert_to_pdf,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=True, # 让它在返回码非 0 时抛出异常
                    encoding='utf-8',
                    errors='ignore'
                )
                logger.debug("LibreOffice 转换 PDF 命令执行完成。")
                if result_pdf.stdout: logger.debug(f"LibreOffice STDOUT:\n{result_pdf.stdout}")
                if result_pdf.stderr: logger.debug(f"LibreOffice STDERR:\n{result_pdf.stderr}")

                if not pdf_output_path.exists() or pdf_output_path.stat().st_size == 0:
                    logger.error(f"LibreOffice 命令执行后未找到或生成了空的 PDF 文件: {pdf_output_path}")
                    task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: PDF file not created or empty'})
                    return None # PDF 文件无效

                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 20, 'status': 'LibreOffice conversion complete'})

            except subprocess.CalledProcessError as e:
                logger.error(f"LibreOffice 转换 PDF 失败。返回码: {e.returncode}")
                logger.error(f"命令: {shlex.join(cmd_convert_to_pdf)}")
                if result_pdf.stdout: logger.error(f"STDOUT:\n{result_pdf.stdout}")
                if result_pdf.stderr: logger.error(f"STDERR:\n{result_pdf.stderr}")
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': f'Error: LibreOffice conversion failed ({e.returncode})', 'stderr': result_pdf.stderr})
                return None
            except subprocess.TimeoutExpired:
                logger.error(f"LibreOffice 转换 PDF 超时 ({timeout_seconds} 秒)。PPT 文件可能过大或复杂。")
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: LibreOffice conversion timed out'})
                return None
            except FileNotFoundError:
                logger.error(f"错误：找不到 LibreOffice 命令 '{libreoffice_path}'。")
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: soffice not found during call'})
                return None
            except Exception as e:
                logger.error(f"执行 LibreOffice 转换时发生未知错误: {e}", exc_info=True)
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': f'Error: LibreOffice conversion failed ({type(e).__name__})'})
                return None

            # 5. 调用 pdf2image 将 PDF 转换为图片
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': 25, 'status': 'Starting PDF to image conversion'})
            logger.debug("开始使用 pdf2image 将 PDF 转换为 PNG 图片...")
            exported_files = []
            try:
                dpi = config.getint('Video', 'image_export_dpi', fallback=150)
                logger.debug(f"使用 DPI: {dpi} 进行 PDF 到图片转换")

                # 获取 PDF 总页数 (用于进度估算)
                num_pages = 0
                try:
                     # pdfinfo_from_path 也需要 Poppler，如果 get_poppler_path 返回 None，它会依赖 PATH
                     pdf_info = pdfinfo_from_path(str(pdf_output_path.resolve()), poppler_path=poppler_path_for_pdf2image)
                     num_pages = pdf_info.get('Pages', 0)
                     logger.debug(f"PDF 总页数: {num_pages}")
                except Exception as page_count_e:
                     logger.warning(f"无法获取 PDF 页数以估算进度: {page_count_e}", exc_info=True)
                     num_pages = 0 # 如果获取页数失败，进度条可能不准确


                images = convert_from_path(
                    pdf_output_path,
                    output_folder=output_dir,
                    fmt='png',
                    output_file="slide_", # PyInstaller 打包后可能需要绝对路径或特殊处理
                    paths_only=True,
                    dpi=dpi,
                    use_pdftocairo=True, # 优先使用 pdftocairo (如果可用)
                    poppler_path=poppler_path_for_pdf2image # 传递获取到的 Poppler 路径
                    # thread_count=... 可以控制并发，需要测试
                )

                # --- 修复文件名和排序 ---
                generated_images = sorted(
                    output_dir.glob("slide_*.png"),
                    key=lambda p: p.stat().st_mtime # 按修改时间排序 (或者按文件名中的数字排序)
                    # key=lambda p: int(re.search(r'slide_(\d+)', p.name).group(1)) # 如果文件名始终是 slide_N.png 格式
                )
                num_converted_images = len(generated_images)
                logger.debug(f"pdf2image 转换并生成了 {num_converted_images} 个图片文件。")

                for i, old_path in enumerate(generated_images):
                    slide_number = i + 1
                    new_filename = f"slide_{slide_number}.png"
                    new_path = output_dir / new_filename
                    try:
                        if new_path.exists() and old_path.resolve() != new_path.resolve():
                            logger.warning(f"目标文件 {new_path.name} 已存在，将被覆盖。")
                            new_path.unlink()

                        # 只在文件名不同时才重命名
                        if old_path.name != new_filename:
                            old_path.rename(new_path)
                            logger.debug(f"  重命名图片: {old_path.name} -> {new_path.name}")
                        else:
                            logger.debug(f"  图片文件名已正确: {new_path.name}")

                        exported_files.append(str(new_path.resolve()))

                        # 更新进度 (pdf2image 进度)
                        if num_pages > 0:
                            progress = 25 + int((i + 1) / num_pages * 60) # 25% 到 85% 用于图片转换
                            task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'progress': progress, 'status': f'Converting image {i+1}/{num_pages}'})


                    except OSError as rename_e:
                        logger.error(f"  重命名图片 {old_path.name} 到 {new_path.name} 失败: {rename_e}")
                        exported_files.append(str(old_path.resolve())) # 即使重命名失败，也尝试添加原始路径

                if len(exported_files) != num_converted_images:
                     logger.warning(f"整理后的图片数量 ({len(exported_files)}) 与转换的图片文件数量 ({num_converted_images}) 不符。")


                if not exported_files:
                    logger.error("未能成功整理任何导出的图片。")
                    task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: No images exported'})
                    return None

                logger.info(f"成功导出并整理了 {len(exported_files)} 张图片。")
                # 最终进度在主任务中更新

            except PDFInfoNotInstalledError:
                logger.error(f"pdf2image 错误: 无法获取 PDF 信息。请确保 Poppler 工具已安装并可在 PATH 或配置路径 '{poppler_path_for_pdf2image}' 中找到。")
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: Poppler not found'})
                return None
            except (PDFSyntaxError, PDFPageCountError) as e:
                 logger.error(f"pdf2image 错误: PDF 文件语法错误或无法获取页数: {e}", exc_info=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': f'Error: Invalid PDF ({type(e).__name__})'})
                 return None
            except PDFPopplerTimeoutError:
                 logger.error("pdf2image 错误: Poppler 转换超时。")
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': 'Error: Poppler timed out'})
                 return None
            except Exception as e:
                logger.error(f"pdf2image 转换 PDF 到图片时出错: {e}", exc_info=True)
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_IMAGES, 'status': f'Error: PDF to image failed ({type(e).__name__})'})
                return None

    except Exception as outer_e:
        # 捕获 with tempfile.TemporaryDirectory 可能的错误，或 LibreOffice 之前的错误
        logger.error(f"处理临时目录或 LibreOffice/pdf2image 导出过程中发生意外错误: {outer_e}", exc_info=True)
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'status': f'Error during PPT processing ({type(outer_e).__name__})'})
        return None
    finally:
        # 临时 PDF 目录会在 with 语句结束时自动清理
        pass # 其他清理在主任务的 finally 块中处理

    return exported_files # 返回导出的图片路径列表