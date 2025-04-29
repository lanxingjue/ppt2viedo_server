// static/js/status_check.js

// 这个文件会被 status.html 引入

document.addEventListener('DOMContentLoaded', function() {
    const taskId = document.getElementById('task-id').textContent.trim();
    const statusText = document.getElementById('task-status');
    const statusMessage = document.getElementById('status-message');
    const downloadLinkDiv = document.getElementById('download-link');
    const videoDownloadUrl = document.getElementById('video-download-url');
    const errorMessageDiv = document.getElementById('error-message');
    const errorDetails = document.getElementById('error-details');

    // 根据状态文本设置颜色和文本
    function updateStatusDisplay(state) {
         let color = '#cccccc'; // Default
         let text = state;
         if (state === 'PENDING') { color = '#ffff00'; text = '排队中'; } // Yellow/Orange
         else if (state === 'STARTED') { color = '#00ffff'; text = '处理中'; } // Cyan
         else if (state === 'PROCESSING') { color = '#00ffff'; text = '处理中'; } // Alias for STARTED in our task
         else if (state === 'SUCCESS') { color = '#00ff00'; text = '完成'; } // Green
         else if (state === 'FAILURE') { color = '#ff0000'; text = '失败'; } // Red
         else if (state === 'REVOKED') { color = '#ff0000'; text = '已取消'; } // Red
         else if (state === 'RETRY') { color = '#ffa500'; text = '重试中'; } // Orange

         statusText.textContent = text;
         statusText.style.color = color;
         statusText.style.textShadow = `0 0 3px ${color}`; // 添加发光效果
    }


    // 轮询任务状态的函数
    function checkStatus() {
        // 构建正确的 API URL
        const statusUrl = `/tasks/${taskId}/status`;

        fetch(statusUrl)
            .then(response => {
                 if (!response.ok) {
                     // 处理非 2xx 响应，比如 404, 500
                     console.error(`HTTP error! status: ${response.status}`);
                     statusMessage.textContent = `无法获取任务状态 (HTTP ${response.status})。请稍后刷新页面。`;
                     statusText.textContent = 'ERROR';
                     statusText.style.color = '#ff0000';
                     statusText.style.textShadow = '0 0 3px #ff0000';
                     throw new Error(`HTTP status ${response.status}`); // 抛出错误终止 promise 链
                 }
                 return response.json();
             })
            .then(data => {
                // console.log("Task status data:", data); // 调试日志

                updateStatusDisplay(data.state); // 更新状态文本和颜色

                // 根据任务状态执行不同操作
                if (data.state === 'PENDING' || data.state === 'STARTED' || data.state === 'PROCESSING' || data.state === 'RETRY') {
                    // 任务仍在进行中，更新详细信息并继续轮询
                    statusMessage.textContent = `状态更新: ${data.state}...`;
                    // 检查是否有任务自定义的 meta 信息，例如进度或当前阶段
                    if (data.meta && data.meta.stage) {
                         statusMessage.textContent += ` (阶段: ${data.meta.stage})`;
                         // TODO: 如果任务发送进度，可以在这里更新进度条
                         // if (data.meta.progress) {
                         //     const progressBarFill = document.getElementById('task-progress');
                         //     if (progressBarFill) progressBarFill.style.width = data.meta.progress + '%';
                         // }
                    }
                    // 继续轮询
                    setTimeout(checkStatus, 2000); // 每 2 秒轮询一次
                } else if (data.state === 'SUCCESS') {
                    // 任务成功完成
                    statusMessage.textContent = '任务处理成功！视频已生成。';
                    downloadLinkDiv.style.display = 'block'; // 显示下载链接区域
                    errorMessageDiv.style.display = 'none'; // 隐藏错误信息区域

                    if (data.download_url) {
                         videoDownloadUrl.href = data.download_url;
                         videoDownloadUrl.textContent = '下载视频'; // 设置下载链接文本
                         // 从 download_url 中提取文件名显示
                         const filename = data.download_url.split('/').pop().split('?')[0]; // 处理可能的查询参数
                         statusMessage.textContent += ` 文件名: ${decodeURIComponent(filename)}`;
                    } else {
                         // 如果没有提供 download_url，尝试使用 result 字段
                         console.warn("API 未提供 download_url, 尝试使用 result 字段。");
                         if (data.result) {
                             // 假设 result 是文件名或相对路径，构造一个简单的下载 URL
                              // 需要确保这里的 URL 构造方式与后端 download_file 路由匹配
                             const assumedDownloadUrl = `/output/${encodeURIComponent(data.result.split('/').pop())}`; // 提取文件名并编码
                             videoDownloadUrl.href = assumedDownloadUrl;
                             videoDownloadUrl.textContent = `下载: ${data.result.split('/').pop()}`;
                              statusMessage.textContent += ` 文件名: ${data.result.split('/').pop()}`;
                         } else {
                            videoDownloadUrl.textContent = '下载链接不可用';
                            console.error("API 未返回 download_url 或 result。");
                         }
                    }
                    // 停止轮询
                } else if (data.state === 'FAILURE' || data.state === 'REVOKED') {
                    // 任务失败或被取消
                    statusMessage.textContent = '任务处理失败或被取消。';
                    downloadLinkDiv.style.display = 'none'; // 隐藏下载链接
                    errorMessageDiv.style.display = 'block'; // 显示错误信息区域
                    errorDetails.textContent = data.error || '服务器返回未知错误。请检查服务器日志获取详细信息。';
                    // 停止轮询
                } else {
                     // 未知状态
                     statusMessage.textContent = `未知任务状态: ${data.state}. 请检查服务器日志。`;
                     statusText.textContent = data.state; // 显示原始状态
                     statusText.style.color = '#ffa500'; // Orange
                     statusText.style.textShadow = '0 0 3px #ffa500';
                     // 可能继续轮询一段时间，或停止
                     setTimeout(checkStatus, 5000); // 间隔长一点
                }
            })
            .catch(error => {
                // 捕获 fetch 请求本身的网络错误等
                console.error('Error checking task status:', error);
                // 如果已经显示了错误信息，不再覆盖
                if (statusText.textContent !== 'ERROR') {
                     statusMessage.textContent = '网络错误，无法获取任务状态。请检查网络连接并稍后刷新页面。';
                     statusText.textContent = 'ERROR';
                     statusText.style.color = '#ff0000';
                     statusText.style.textShadow = '0 0 3px #ff0000';
                }
                // 不再继续轮询
            });
        }

        // 页面加载后开始轮询 (给浏览器一个初始显示时间)
        setTimeout(checkStatus, 500);

}); // DOMContentLoaded end