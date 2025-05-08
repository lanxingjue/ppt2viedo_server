// static/js/status_check.js

document.addEventListener('DOMContentLoaded', function() {
    const taskId = document.getElementById('task-id').textContent.trim();
    const statusText = document.getElementById('task-status'); // 任务状态文本元素 (如 "PENDING", "STARTED")
    const statusStage = document.getElementById('status-stage'); // 任务阶段文本元素

    const downloadLinkDiv = document.getElementById('download-link');
    const videoDownloadUrl = document.getElementById('video-download-url');
    const completionTime = document.getElementById('completion-time');

    const errorMessageDiv = document.getElementById('error-message');
    const errorSummary = document.getElementById('error-summary'); // 错误概要文本元素
    const errorDetails = document.getElementById('error-details'); // 详细错误/堆栈跟踪文本元素
    const showTracebackButton = document.getElementById('show-traceback'); // 显示详细信息按钮

    // 初始化状态显示
    // 从后端渲染的 initial_status 获取初始状态
    updateStatusDisplay(statusText.textContent);
    // 隐藏详细错误部分
    errorDetails.style.display = 'none';
    showTracebackButton.style.display = 'none';


    // 根据状态文本设置颜色和文本
    function updateStatusDisplay(state) {
         let color = '#cccccc'; // Default
         let text = state;
         if (state === 'PENDING') { color = '#ffff00'; text = '排队中'; }
         else if (state === 'STARTED' || state === 'PROCESSING') { color = '#00ffff'; text = '处理中'; }
         else if (state === 'SUCCESS') { color = '#00ff00'; text = '完成'; }
         else if (state === 'FAILURE') { color = '#ff0000'; text = '失败'; }
         else if (state === 'REVOKED') { color = '#ff0000'; text = '已取消'; }
         else if (state === 'RETRY') { color = '#ffa500'; text = '重试中'; }

         statusText.textContent = text;
         statusText.style.color = color;
         statusText.style.textShadow = `0 0 3px ${color}`;
    }


    // 轮询任务状态的函数
    function checkStatus() {
        const statusUrl = `/tasks/${taskId}/status`;

        fetch(statusUrl)
            .then(response => {
                 if (!response.ok) {
                     console.error(`HTTP error! status: ${response.status}`);
                     // 如果 HTTP 错误，更新状态显示，不再轮询
                     statusStage.textContent = `无法获取任务状态 (HTTP ${response.status})。`;
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = `网络或服务器错误，HTTP 状态码: ${response.status}`;
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                     throw new Error(`HTTP status ${response.status}`);
                 }
                 return response.json();
             })
            .then(data => {
                // console.log("Task status data:", data); // 调试日志

                updateStatusDisplay(data.state); // 更新状态文本和颜色

                // 更新阶段信息 (从 meta 中获取)
                if (data.meta && data.meta.stage) {
                     statusStage.textContent = `阶段: ${data.meta.stage}`;
                     // TODO: 如果需要更细粒度的状态信息（如当前文件），可以在 meta 里增加并在这里显示
                     // if (data.meta.current_file) {
                     //     statusStage.textContent += ` (${data.meta.current_file})`;
                     // }
                } else if (data.state === 'PENDING') {
                     statusStage.textContent = '阶段: 排队等待中...';
                } else {
                     // 对于 STARTED/PROCESSING 但没有 meta.stage 的情况，显示通用信息
                     statusStage.textContent = '阶段: 处理中...';
                }


                // TODO: 如果任务发送进度，可以在这里更新进度条和百分比
                // if (data.meta && data.meta.progress !== undefined) {
                //     const progress = Math.max(0, Math.min(100, data.meta.progress)); // 确保进度在 0-100
                //     if (taskProgressFill) taskProgressFill.style.width = progress + '%';
                //     if (progressPercentage) progressPercentage.textContent = Math.round(progress) + '%';
                // }


                // 根据任务状态执行不同操作
                if (data.state === 'PENDING' || data.state === 'STARTED' || data.state === 'PROCESSING' || data.state === 'RETRY') {
                    // 任务仍在进行中，继续轮询
                    // 只有在这些状态下才继续设置定时器
                    setTimeout(checkStatus, 2000); // 每 2 秒轮询一次

                } else if (data.state === 'SUCCESS') {
                    // 任务成功完成
                    statusStage.textContent = '阶段: 完成';
                    downloadLinkDiv.style.display = 'block'; // 显示下载链接区域
                    errorMessageDiv.style.display = 'none'; // 隐藏错误信息区域
                    showTracebackButton.style.display = 'none'; // 隐藏按钮

                    // 获取并设置下载链接
                    if (data.download_url) {
                         videoDownloadUrl.href = data.download_url;
                         const filename = decodeURIComponent(data.download_url.split('/').pop().split('?')[0]);
                         videoDownloadUrl.textContent = `下载视频: ${filename}`;
                    } else if (data.result) {
                         // 如果没有 download_url，尝试使用 result 字段构造
                         const assumedFilename = Path(data.result).name;
                         const assumedDownloadUrl = `/output/${encodeURIComponent(assumedFilename)}`;
                         videoDownloadUrl.href = assumedDownloadUrl;
                         videoDownloadUrl.textContent = `下载视频: ${assumedFilename}`;
                         console.warn("API 未提供 download_url，尝试使用 result 字段构造下载链接。");
                    } else {
                         videoDownloadUrl.textContent = '下载链接不可用';
                         console.error("API 未返回 download_url 或 result。");
                         // 虽然任务状态是 SUCCESS，但没有下载链接，视为异常
                         errorSummary.textContent = "任务成功完成，但未返回有效的视频文件信息。请检查服务器日志。";
                         errorMessageDiv.style.display = 'block';
                         downloadLinkDiv.style.display = 'none';
                    }

                    // 显示总耗时（如果 meta 中有）
                    if (data.meta && data.meta.duration !== undefined) {
                        completionTime.textContent = `总耗时: ${data.meta.duration.toFixed(2)} 秒`;
                    } else {
                         completionTime.textContent = `任务完成`;
                    }


                    // 停止轮询 (不需要再设置定时器)

                } else if (data.state === 'FAILURE' || data.state === 'REVOKED') {
                    // 任务失败或被取消
                    statusStage.textContent = '阶段: 失败';
                    downloadLinkDiv.style.display = 'none'; // 隐藏下载链接
                    errorMessageDiv.style.display = 'block'; // 显示错误信息区域
                    // 隐藏详细信息区域，只显示概要
                    errorDetails.style.display = 'none';


                    // 显示错误信息 (从 meta 中获取)
                    if (data.meta && data.meta.error) {
                        errorSummary.textContent = data.meta.error; // 显示任务中记录的错误概要

                        // 显示详细堆栈跟踪（如果存在）
                        if (data.meta.traceback) {
                            errorDetails.textContent = data.meta.traceback;
                            showTracebackButton.style.display = 'inline-block'; // 显示按钮
                             // 绑定按钮点击事件 (确保只绑定一次)
                             if (!showTracebackButton.onclick) {
                                 showTracebackButton.onclick = function() {
                                     if (errorDetails.style.display === 'none') {
                                         errorDetails.style.display = 'block';
                                         showTracebackButton.textContent = '隐藏详细信息';
                                     } else {
                                         errorDetails.style.display = 'none';
                                         showTracebackButton.textContent = '显示详细信息';
                                     }
                                 };
                             }


                        } else {
                            // 没有堆栈跟踪，隐藏按钮
                            showTracebackButton.style.display = 'none';
                        }

                    } else if (data.error) {
                        // 如果 meta 中没有详细错误信息，但顶级有 error 字段 (旧版 Celery 或其他情况)
                        errorSummary.textContent = data.error;
                        showTracebackButton.style.display = 'none';
                    }
                     else {
                        // 如果没有错误信息，显示通用信息
                        errorSummary.textContent = '服务器返回未知错误。请检查服务器日志获取详细信息。';
                        showTracebackButton.style.display = 'none';
                    }

                    // 停止轮询 (不需要再设置定时器)
                } else {
                     // 未知状态 - 可能还在处理中，或者进入了非标准状态
                     statusStage.textContent = `未知状态: ${data.state}`;
                     updateStatusDisplay(data.state);
                     // 继续轮询一段时间，或根据需要调整逻辑
                     setTimeout(checkStatus, 5000); // 间隔长一点
                }
            })
            .catch(error => {
                // 捕获 fetch 请求本身的网络错误等
                console.error('Error checking task status:', error);
                // 如果已经显示了错误信息，不再覆盖
                if (statusText.textContent !== 'ERROR' && statusText.textContent !== '失败') {
                     statusStage.textContent = '网络错误，无法获取任务状态。';
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = '网络连接错误或服务器问题，请检查服务器状态并稍后刷新页面。';
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                }
                // 停止轮询
            });
        }

    // 页面加载后开始轮询 (给浏览器一个初始显示时间)
    // 初始检查不需要延迟，但第一次更新可能需要立即发生
    // checkStatus(true); // 初始检查
    // 更好的方式是根据初始状态判断是否需要开始轮询
    // 如果初始状态不是最终状态 (SUCCESS, FAILURE, REVOKED)，则开始轮询
    const initialStatus = statusText.textContent;
    if (initialStatus !== 'SUCCESS' && initialStatus !== 'FAILURE' && initialStatus !== 'ERROR' && initialStatus !== 'REVOKED' && initialStatus !== '完成' && initialStatus !== '失败' && initialStatus !== '已取消') {
         setTimeout(checkStatus, 500); // 稍作延迟开始轮询
    } else {
         // 如果初始状态是最终状态，直接尝试填充错误或下载链接 (需要从 initial_meta 中获取)
         // 这个逻辑需要额外实现，或者依赖用户刷新页面
         // 暂时不实现，用户刷新页面即可看到最终结果
         console.log(`Task ${taskId} initial status is final: ${initialStatus}. Stopping polling.`);
    }


}); // DOMContentLoaded end