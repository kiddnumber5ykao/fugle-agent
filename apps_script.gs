/**
 * 加油好嗎 — Sheets Writer Web App
 *
 * 把這份 code 貼到「加油好嗎」Google Sheet 綁定的 Apps Script,
 * 部署成 Web App,就能讓 agent 直接寫入你的試算表。
 *
 * 支援的 actions:
 *   ping              健康檢查
 *   add_trade         在「股票交易」分頁新增一行
 *   add_fund_trade    在「基金交易」分頁新增一行
 *   upsert_position   新增 / 更新「股票部位」分頁的一行(symbol 為 key)
 *   delete_position   刪除「股票部位」分頁的一行
 *   upsert_fund       新增 / 更新「基金部位」分頁的一行(fund_id 為 key)
 *   delete_fund       刪除「基金部位」分頁的一行
 *   update_trade_realized 把單筆 SELL 的 realized_pnl 寫回「股票交易」對應 row
 *                          (用 date + symbol + action + shares 找到那一列)
 *   add_etf_snapshot       在「ETF快照」分頁新增一行(持股快照,一檔股票一 row)
 *   add_watchlist_item     在「追蹤清單」分頁新增一行(symbol 為 key)
 *   delete_watchlist_item  刪除「追蹤清單」一行
 *
 * 請求格式(POST JSON):
 *   { "action": "add_trade", "args": { "date": "...", ... } }
 *
 * 回應格式:
 *   { "ok": true, ... } 或 { "ok": false, "error": "..." }
 */

const POSITIONS_TAB     = '股票部位';
const TRADES_TAB        = '股票交易';
const FUNDS_TAB         = '基金部位';
const FUND_TRADES_TAB   = '基金交易';
const ETF_SNAPSHOTS_TAB = 'ETF快照';
const WATCHLIST_TAB     = '追蹤清單';

// =========================================================================
// HTTP entry points
// =========================================================================

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonResponse({ ok: false, error: 'Invalid JSON' });
  }
  const action = String(body.action || '').trim();
  const args = body.args || {};

  try {
    switch (action) {
      case 'ping':             return jsonResponse({ ok: true, pong: new Date().toISOString() });
      case 'add_trade':        return doAddRow(TRADES_TAB, args);
      case 'add_fund_trade':   return doAddRow(FUND_TRADES_TAB, args);
      case 'upsert_position':  return doUpsertRow(POSITIONS_TAB, 'symbol',  args);
      case 'delete_position':  return doDeleteRow(POSITIONS_TAB, 'symbol',  args.symbol);
      case 'upsert_fund':      return doUpsertRow(FUNDS_TAB,     'fund_id', args);
      case 'delete_fund':      return doDeleteRow(FUNDS_TAB,     'fund_id', args.fund_id);
      case 'update_trade_realized': return doUpdateTradeRealized(TRADES_TAB, args);
      case 'add_etf_snapshot':    return doAddRow(ETF_SNAPSHOTS_TAB, args);
      case 'add_watchlist_item':  return doAddRow(WATCHLIST_TAB, args);
      case 'delete_watchlist_item': return doDeleteRow(WATCHLIST_TAB, 'symbol', args.symbol);
      default:
        return jsonResponse({ ok: false, error: 'Unknown action: ' + action });
    }
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err && err.message || err) });
  }
}

function doGet(e) {
  return jsonResponse({ ok: true, message: '加油好嗎 sheets writer is alive 🤖' });
}

// =========================================================================
// Helpers
// =========================================================================

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function getSheet(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(name);
  if (!sheet) throw new Error('找不到分頁: ' + name);
  return sheet;
}

function getHeaders(sheet) {
  const last = sheet.getLastColumn();
  if (last < 1) return [];
  return sheet.getRange(1, 1, 1, last).getValues()[0].map(String).map(s => s.trim());
}

function findRowByKey(sheet, keyCol, keyValue) {
  const headers = getHeaders(sheet);
  const keyIdx = headers.indexOf(keyCol);
  if (keyIdx === -1) throw new Error('找不到欄位: ' + keyCol);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return -1;
  const col = sheet.getRange(2, keyIdx + 1, lastRow - 1, 1).getValues();
  for (let i = 0; i < col.length; i++) {
    if (String(col[i][0]).trim() === String(keyValue).trim()) {
      return i + 2; // 1-indexed; +1 for header row
    }
  }
  return -1;
}

// 避免 Sheets 把 '0050' 吃成 50 — 對 symbol / fund_id 欄前綴 ' 強制文字
function isCodeColumn(h) {
  return h === 'symbol' || h === 'fund_id';
}
function asTextIfCode(h, v) {
  if (!isCodeColumn(h)) return v;
  if (v === undefined || v === null || v === '') return v;
  const s = String(v);
  return s.startsWith("'") ? s : "'" + s;
}

function rowFromArgs(headers, args) {
  return headers.map(h => {
    const v = args[h];
    if (v === undefined || v === null) return '';
    return asTextIfCode(h, v);
  });
}

// =========================================================================
// Action implementations
// =========================================================================

function doAddRow(tabName, args) {
  const sheet = getSheet(tabName);
  const headers = getHeaders(sheet);
  sheet.appendRow(rowFromArgs(headers, args));
  return jsonResponse({ ok: true, action: 'add_row', tab: tabName, args: args });
}

function doUpsertRow(tabName, keyCol, args) {
  const sheet = getSheet(tabName);
  const headers = getHeaders(sheet);
  const keyValue = args[keyCol];
  if (!keyValue) return jsonResponse({ ok: false, error: '缺少 ' + keyCol });

  const rowNum = findRowByKey(sheet, keyCol, keyValue);
  if (rowNum === -1) {
    // Insert
    sheet.appendRow(rowFromArgs(headers, args));
    return jsonResponse({ ok: true, action: 'insert', tab: tabName, key: keyValue });
  }
  // Update — only overwrite fields the caller provided
  headers.forEach(function (h, idx) {
    if (args[h] !== undefined) {
      sheet.getRange(rowNum, idx + 1).setValue(asTextIfCode(h, args[h]));
    }
  });
  return jsonResponse({ ok: true, action: 'update', tab: tabName, key: keyValue });
}

function doDeleteRow(tabName, keyCol, keyValue) {
  const sheet = getSheet(tabName);
  const rowNum = findRowByKey(sheet, keyCol, keyValue);
  if (rowNum === -1) {
    return jsonResponse({ ok: false, error: '找不到 ' + keyCol + ' = ' + keyValue });
  }
  sheet.deleteRow(rowNum);
  return jsonResponse({ ok: true, action: 'delete', tab: tabName, key: keyValue });
}

// 把任何日期表示(Date 物件 / "2026/05/21" / "2026-05-21" / "2026-5-21")正規成 "2026-05-21"
function _normDate(v) {
  if (v instanceof Date) {
    return Utilities.formatDate(v, 'GMT+8', 'yyyy-MM-dd');
  }
  const s = String(v == null ? '' : v).trim();
  if (!s) return '';
  // 把 / 改成 -,然後抓前 10 字元
  const dashed = s.replace(/\//g, '-').slice(0, 10);
  // 補零 — "2026-5-1" → "2026-05-01"
  const m = dashed.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    const yyyy = m[1];
    const mm = ('0' + m[2]).slice(-2);
    const dd = ('0' + m[3]).slice(-2);
    return yyyy + '-' + mm + '-' + dd;
  }
  return dashed;
}

// 把單筆 SELL 的 realized_pnl 寫回「股票交易」對應 row。
// 用 date + symbol + action + shares 當複合鍵找列(這四個值通常唯一)。
// realized_pnl 欄位名可以是英文 realized_pnl 或中文「已實現損益」,有哪欄就寫哪欄。
function doUpdateTradeRealized(tabName, args) {
  const sheet = getSheet(tabName);
  const headers = getHeaders(sheet);
  const dateIdx = headers.indexOf('date');
  const dateIdxZh = headers.indexOf('日期');
  const symIdx  = headers.indexOf('symbol');
  const symIdxZh = headers.indexOf('代號');
  const actIdx  = headers.indexOf('action');
  const actIdxZh = headers.indexOf('動作');
  const shIdx   = headers.indexOf('shares');
  const shIdxZh = headers.indexOf('股數');

  const dIdx = dateIdx >= 0 ? dateIdx : dateIdxZh;
  const sIdx = symIdx  >= 0 ? symIdx  : symIdxZh;
  const aIdx = actIdx  >= 0 ? actIdx  : actIdxZh;
  const hIdx = shIdx   >= 0 ? shIdx   : shIdxZh;

  if (dIdx < 0 || sIdx < 0 || aIdx < 0 || hIdx < 0) {
    return jsonResponse({ ok: false,
      error: '「股票交易」缺必要欄位 date/symbol/action/shares(或對應中文)' });
  }

  // realized_pnl 寫到所有命中的欄(英文 + 中文都有就兩邊都寫)
  const realizedCols = [];
  ['realized_pnl', '已實現損益'].forEach(function(name) {
    const idx = headers.indexOf(name);
    if (idx >= 0) realizedCols.push(idx);
  });
  if (realizedCols.length === 0) {
    return jsonResponse({ ok: false,
      error: '「股票交易」沒有 realized_pnl 或 已實現損益 欄位 — 請先新增至少一欄'
    });
  }

  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return jsonResponse({ ok: false, error: '股票交易沒有資料列' });

  const values = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
  const tDate = _normDate(args.date);
  const tSym  = String(args.symbol || '').replace(/^'/, '').trim();
  const tAct  = String(args.action || '').toUpperCase().trim();
  const tSh   = Number(args.shares || 0);

  for (let i = 0; i < values.length; i++) {
    const r = values[i];
    const d  = _normDate(r[dIdx]);
    const s  = String(r[sIdx] || '').replace(/^'/, '').trim();
    const a  = String(r[aIdx] || '').toUpperCase().trim();
    const sh = Number(r[hIdx] || 0);
    if (d === tDate && s === tSym && a === tAct && sh === tSh) {
      const rowNum = i + 2;
      realizedCols.forEach(function(idx) {
        sheet.getRange(rowNum, idx + 1).setValue(args.realized_pnl);
      });
      return jsonResponse({ ok: true, action: 'update_trade_realized',
                             row: rowNum, realized_pnl: args.realized_pnl });
    }
  }
  return jsonResponse({ ok: false,
    error: '找不到符合的 SELL: date=' + tDate + ' symbol=' + tSym +
           ' action=' + tAct + ' shares=' + tSh });
}
