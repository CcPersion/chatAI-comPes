/**
 * =============================================================================
 * avatar-sync.js — Node.js 音频转发 + 数字人背景层
 * =============================================================================
 * 职责:
 *   1. 监听 :8011，接收 speech 管线的 TTS 音频并转发到 LiveTalking (:8010)
 *   2. WebSocket 信令中继：将 LiveTalking 的 WebRTC 视频流提供给浏览器
 *   3. GET /health 探活端点
 *
 * 运行: node avatar-sync.js
 * 依赖: npm install express ws multer (见 outputs/backend/待补充)
 * =============================================================================
 */

'use strict';

// ---- 配置 ----
const CONFIG = {
    port: 8011,
    // WSL2 port forwarding targets the distro network address. Keep the
    // Windows-facing portproxy bound to 127.0.0.1; the bridge must accept
    // traffic on the WSL interface here.
    host: '0.0.0.0',
    livetalkingUrl: 'http://127.0.0.1:8010',
    maxAudioSize: 10 * 1024 * 1024, // 10 MB 单段音频上限
};

// ---- 依赖 ----
const http = require('http');
const crypto = require('crypto');
const { Readable } = require('stream');

// =========================================================================
// 简易 HTTP 服务器（无外部框架依赖，Express 为可选优化）
// 若需要更健壮的 multipart 解析，安装 express + multer：
//   npm install express ws multer
// 并替换本文件中的简易实现。
// =========================================================================

/**
 * 简易 multipart/form-data 解析器
 * 仅支持单个 audio 字段的 WAV/PCM 上传。
 * 生产环境建议使用 multer。
 */
function parseSimpleMultipart(contentType, body) {
    const boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/);
    if (!boundaryMatch) return null;
    const boundary = boundaryMatch[1] || boundaryMatch[2];
    const boundaryDelim = '--' + boundary;
    const boundaryEnd = boundaryDelim + '--';

    const parts = body.split(boundaryDelim);
    for (const part of parts) {
        if (part.includes('Content-Disposition')) {
            const headerEnd = part.indexOf('\r\n\r\n');
            if (headerEnd === -1) continue;
            const headers = part.substring(0, headerEnd);
            const content = part.substring(headerEnd + 4);

            // 去掉末尾的 \r\n 和 boundary 尾部
            const cleanContent = content.replace(/\r\n$/, '');

            if (headers.includes('name="audio"')) {
                return {
                    filename: (headers.match(/filename="([^"]+)"/) || ['', 'audio.wav'])[1],
                    contentType: (headers.match(/Content-Type:\s*(\S+)/) || ['', 'audio/wav'])[1],
                    data: Buffer.from(cleanContent, 'binary'),
                };
            }
        }
    }
    return null;
}

/**
 * 解析 JSON body（用于 base64 音频传输）
 */
function parseJsonBody(bodyStr) {
    try {
        return JSON.parse(bodyStr);
    } catch {
        return null;
    }
}

/**
 * 生成唯一 session ID
 */
function generateSessionId() {
    return crypto.randomUUID();
}

/**
 * 检查 LiveTalking 上游是否健康
 */
function checkUpstream() {
    return new Promise((resolve) => {
        const req = http.get(
            `${CONFIG.livetalkingUrl}/health`,
            { timeout: 3000 },
            (res) => {
                let body = '';
                res.on('data', (chunk) => { body += chunk; });
                res.on('end', () => {
                    try {
                        const data = JSON.parse(body);
                        resolve(data.status === 'ok' ? 'ok' : 'degraded');
                    } catch {
                        resolve('degraded');
                    }
                });
            }
        );
        req.on('error', () => resolve('unreachable'));
        req.on('timeout', () => {
            req.destroy();
            resolve('timeout');
        });
    });
}

/**
 * 转发音频到 LiveTalking POST /humanaudio
 */
function forwardToLiveTalking(audioBuffer, sampleRate = 16000) {
    return new Promise((resolve, reject) => {
        const boundary = '----FormBoundary' + crypto.randomBytes(16).toString('hex');
        const sessionPart = `--${boundary}\r\nContent-Disposition: form-data; name="sessionid"\r\n\r\n0\r\n`;
        const fileHeader = `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="tts_chunk.wav"\r\nContent-Type: audio/wav\r\n\r\n`;
        const footer = `\r\n--${boundary}--\r\n`;

        const body = Buffer.concat([
            Buffer.from(sessionPart, 'utf-8'),
            Buffer.from(fileHeader, 'utf-8'),
            audioBuffer,
            Buffer.from(footer, 'utf-8'),
        ]);

        const url = new URL('/humanaudio', CONFIG.livetalkingUrl);
        const options = {
            hostname: url.hostname,
            port: url.port,
            path: url.pathname,
            method: 'POST',
            headers: {
                'Content-Type': `multipart/form-data; boundary=${boundary}`,
                'Content-Length': Buffer.byteLength(body),
                'X-Audio-Sample-Rate': String(sampleRate),
            },
            timeout: 10000,
        };

        const startTime = Date.now();
        const req = http.request(options, (res) => {
            let respBody = '';
            res.on('data', (chunk) => { respBody += chunk; });
            res.on('end', () => {
                const latency = Date.now() - startTime;
                resolve({ ok: res.statusCode === 200, status: res.statusCode, latency_ms: latency, body: respBody });
            });
        });
        req.on('error', (err) => reject(err));
        req.on('timeout', () => {
            req.destroy();
            reject(new Error('LiveTalking upstream timeout'));
        });
        req.write(body);
        req.end();
    });
}

// =========================================================================
// HTTP 请求路由
// =========================================================================
const server = http.createServer(async (req, res) => {
    // CORS 头
    res.setHeader('Access-Control-Allow-Origin', 'http://localhost:7860');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Session-Id, X-Audio-Sample-Rate');

    if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
    }

    const reqUrl = new URL(req.url, `http://${CONFIG.host}:${CONFIG.port}`);

    // ---- GET /health ----
    if (req.method === 'GET' && reqUrl.pathname === '/health') {
        try {
            const upstreamStatus = await checkUpstream();
            const response = {
                status: 'ok',
                service: 'avatar-sync',
                upstream: `livetalking:${upstreamStatus}`,
            };
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(response));
        } catch (err) {
            // 不暴露堆栈（SEC-11）
            const response = {
                status: 'ok',
                service: 'avatar-sync',
                upstream: 'livetalking:unreachable',
            };
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(response));
        }
        return;
    }

    // ---- POST /api/audio ----
    if (req.method === 'POST' && reqUrl.pathname === '/api/audio') {
        const contentType = req.headers['content-type'] || '';
        const sessionId = req.headers['x-session-id'] || generateSessionId();
        const sampleRate = parseInt(req.headers['x-audio-sample-rate'] || '16000', 10);

        const chunks = [];
        req.on('data', (chunk) => chunks.push(chunk));
        req.on('end', async () => {
            const rawBody = Buffer.concat(chunks);
            let audioBuffer = null;

            // 解析 multipart
            if (contentType.includes('multipart/form-data')) {
                const parsed = parseSimpleMultipart(contentType, rawBody.toString('binary'));
                if (parsed && parsed.data) {
                    audioBuffer = parsed.data;
                }
            }

            // 解析 JSON（base64 编码音频）
            if (!audioBuffer && contentType.includes('application/json')) {
                const json = parseJsonBody(rawBody.toString('utf-8'));
                if (json && json.audio_base64) {
                    audioBuffer = Buffer.from(json.audio_base64, 'base64');
                }
            }

            // 直接二进制（无 multipart 包装）
            if (!audioBuffer && contentType.includes('application/octet-stream')) {
                audioBuffer = rawBody;
            }

            if (!audioBuffer || audioBuffer.length === 0) {
                res.writeHead(400, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({
                    error: { code: 'AUDIO_ERR_001', message: '未接收到有效音频数据' }
                }));
                return;
            }

            // 大小上限检查
            if (audioBuffer.length > CONFIG.maxAudioSize) {
                res.writeHead(400, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({
                    error: { code: 'AUDIO_ERR_002', message: '音频数据超出大小上限 (10MB)' }
                }));
                return;
            }

            try {
                console.log(`[avatar-sync] 转发音频到 LiveTalking /humanaudio (session=${sessionId}, ${audioBuffer.length} 字节)`);
                const result = await forwardToLiveTalking(audioBuffer, sampleRate);
                console.log(`[avatar-sync] LiveTalking 响应: ok=${result.ok} status=${result.status} latency=${result.latency_ms}ms`);
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({
                    ok: true,
                    accepted: result.ok,
                    latency_ms: result.latency_ms,
                    session_id: sessionId,
                }));
            } catch (err) {
                // SEC-11: 不暴露内部错误详情
                console.error(`[avatar-sync] LiveTalking 转发失败: ${err.message}`);
                res.writeHead(502, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({
                    error: {
                        code: 'LIP_ERR_001',
                        message: '口型驱动异常，数字人画面可能停用',
                    }
                }));
            }
        });
        return;
    }

    // ---- 404 ----
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
        error: { code: 'NOT_FOUND', message: '端点不存在' }
    }));
});

// =========================================================================
// WebSocket 信令中继（WebRTC 视频流）
// =========================================================================
// 连接 LiveTalking 的 WebSocket，中继给浏览器的 WebRTC 客户端。
// 简易实现：将 avatar-sync 作为信令代理，转发 SDP offer/answer 和 ICE candidate。

const { Server: WebSocketServer } = require('ws');
const wss = new WebSocketServer({ server }); // 复用 HTTP server

wss.on('connection', (browserWs, req) => {
    const clientId = crypto.randomUUID().substring(0, 8);
    console.log(`[avatar-sync] WebSocket 客户端连接: ${clientId}`);

    // 连接到 LiveTalking 的 WebSocket（信令中继）
    let livetalkingWs = null;

    try {
        const ltWsUrl = CONFIG.livetalkingUrl.replace('http://', 'ws://') + '/ws';
        livetalkingWs = new (require('ws'))(ltWsUrl);

        livetalkingWs.on('open', () => {
            console.log(`[avatar-sync] 已连接 LiveTalking WebSocket (client: ${clientId})`);
        });

        // LiveTalking → 浏览器：转发视频流信令
        livetalkingWs.on('message', (data) => {
            if (browserWs.readyState === 1) { // OPEN
                browserWs.send(data.toString());
            }
        });

        // 浏览器 → LiveTalking：转发信令
        browserWs.on('message', (data) => {
            if (livetalkingWs && livetalkingWs.readyState === 1) {
                livetalkingWs.send(data.toString());
            }
        });

        livetalkingWs.on('close', (code) => {
            console.log(`[avatar-sync] LiveTalking WebSocket 已关闭 (code: ${code})`);
            if (browserWs.readyState === 1) {
                browserWs.close(1011, 'LiveTalking 连接已关闭');
            }
        });

        livetalkingWs.on('error', (err) => {
            console.error(`[avatar-sync] LiveTalking WebSocket 错误: ${err.message}`);
            if (browserWs.readyState === 1) {
                browserWs.send(JSON.stringify({
                    type: 'error',
                    error: { code: 'WRT_ERR_001', message: '数字人画面连接失败，请刷新页面' }
                }));
            }
        });

    } catch (err) {
        console.error(`[avatar-sync] 无法连接 LiveTalking WebSocket: ${err.message}`);
        if (browserWs.readyState === 1) {
            browserWs.send(JSON.stringify({
                type: 'error',
                error: { code: 'WRT_ERR_001', message: '数字人画面连接失败，请刷新页面' }
            }));
        }
    }

    browserWs.on('close', () => {
        console.log(`[avatar-sync] 浏览器客户端断开: ${clientId}`);
        if (livetalkingWs && livetalkingWs.readyState === 1) {
            livetalkingWs.close();
        }
    });

    browserWs.on('error', (err) => {
        console.error(`[avatar-sync] 浏览器 WebSocket 错误 (${clientId}): ${err.message}`);
        if (livetalkingWs && livetalkingWs.readyState === 1) {
            livetalkingWs.close();
        }
    });
});

// =========================================================================
// 启动
// =========================================================================
server.listen(CONFIG.port, CONFIG.host, () => {
    console.log(`[avatar-sync] 服务已启动: http://${CONFIG.host}:${CONFIG.port}`);
    console.log(`[avatar-sync] 上游 LiveTalking: ${CONFIG.livetalkingUrl}`);
    console.log(`[avatar-sync] 端点:`);
    console.log(`  GET  /health     — 探活`);
    console.log(`  POST /api/audio  — 接收 TTS 音频并转发驱动口型`);
    console.log(`  WS   /ws         — WebRTC 信令中继`);
});

// 优雅退出
process.on('SIGINT', () => {
    console.log('\n[avatar-sync] 正在关闭...');
    wss.close(() => {
        server.close(() => {
            process.exit(0);
        });
    });
});

process.on('SIGTERM', () => {
    console.log('\n[avatar-sync] 收到 SIGTERM，正在关闭...');
    wss.close(() => {
        server.close(() => {
            process.exit(0);
        });
    });
});
