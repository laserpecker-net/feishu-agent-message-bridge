#!/usr/bin/env node
// as_bot 的收消息侧：用 WebSocket 长连接订阅 im.message.receive_v1，
// 把事件 POST 到本机桥的 /v1/ingest（只回环）。**不做业务判断**：谁该不该回、怎么回，
// 全在桥里（同一套 as_user 的下游），这里只负责"把事件送到"。
//
// 为什么是长连接而不是 webhook：webhook 要求一个公网可达的 HTTPS 入口（域名+证书+签名校验），
// 而长连接不需要任何入站端口，家用/办公内网的机器也能用（见设计稿 §2）。
import { readFileSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const APP_ID = process.env.FEISHU_APP_ID || '';
const APP_SECRET = process.env.FEISHU_APP_SECRET || '';
const INGEST_URL = process.env.FEISHU_INGEST_URL || 'http://127.0.0.1:8787/v1/ingest';
const TOKEN_FILE = process.env.FEISHU_INGEST_TOKEN_FILE || '';
const LOG_LEVEL = (process.env.FEISHU_LOG_LEVEL || 'info').toLowerCase();

const levels = { error: 0, warn: 1, info: 2, debug: 3 };
function log(level, ...args) {
  if ((levels[level] ?? 2) <= (levels[LOG_LEVEL] ?? 2)) {
    console.log(`[sidecar ${new Date().toISOString()}]`, ...args);
  }
}

function readToken() {
  if (!TOKEN_FILE) return '';
  try {
    return readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch (error) {
    log('warn', `cannot read ingest token file ${TOKEN_FILE}: ${error.message}`);
    return '';
  }
}

let token = readToken();
const parsed = new URL(INGEST_URL);
const transport = parsed.protocol === 'https:' ? https : http;

// 只接受 127.0.0.1/::1：这条通道是"同机推送"，不该被外部访问
const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);
if (!LOOPBACK.has(parsed.hostname)) {
  log('error', `refusing non-loopback ingest host: ${parsed.hostname}`);
  process.exit(2);
}

function postJSON(payload) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const request = transport.request(
      {
        hostname: parsed.hostname,
        port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
        path: parsed.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': body.length,
          Authorization: `Bearer ${token}`,
        },
        timeout: 15000,
      },
      (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if (response.statusCode >= 200 && response.statusCode < 300) {
            resolve(text);
          } else if (response.statusCode === 401) {
            token = readToken(); // token 轮换后自动重读
            reject(new Error(`ingest unauthorized (401): ${text}`));
          } else {
            reject(new Error(`ingest failed (${response.statusCode}): ${text}`));
          }
        });
      },
    );
    request.on('timeout', () => request.destroy(new Error('ingest timeout')));
    request.on('error', reject);
    request.write(body);
    request.end();
  });
}

async function main() {
  if (!APP_ID || !APP_SECRET) {
    log('error', 'FEISHU_APP_ID / FEISHU_APP_SECRET are required');
    process.exit(2);
  }
  let sdk;
  try {
    sdk = await import('@larksuiteoapi/node-sdk');
  } catch (error) {
    log('error', 'missing dependency: npm i --omit=dev in sidecar/ ' +
      '(@larksuiteoapi/node-sdk)');
    process.exit(3);
  }

  const client = new sdk.Client({ appId: APP_ID, appSecret: APP_SECRET });
  const dispatcher = new sdk.EventDispatcher({}).register({
    'im.message.receive_v1': async (event) => {
      const messageId = event?.message?.message_id;
      if (!messageId) {
        log('warn', 'event without message_id, ignored');
        return;
      }
      try {
        const result = await postJSON({ event });
        log('info', `forwarded ${messageId}: ${result}`);
      } catch (error) {
        // 送不进去就记下来：桥那边靠 collect() 对账补窗口，这里不重试刷屏
        log('error', `forward failed for ${messageId}: ${error.message}`);
      }
    },
  });

  const wsClient = new sdk.WSClient({ appId: APP_ID, appSecret: APP_SECRET });
  await wsClient.start({ eventDispatcher: dispatcher });
  log('info', 'websocket long connection started');

  const shutdown = () => {
    log('info', 'shutting down');
    try {
      wsClient.close?.();
    } catch (error) {
      log('warn', `close failed: ${error.message}`);
    }
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((error) => {
  log('error', `fatal: ${error.message}`);
  process.exit(1);
});
