// Per-mind batching queue for iframe console logs that are bound for the
// mind's workspace server.
//
// Transport model: Electron main POSTs batches to
// `http://<mind-id>.localhost:<port>/api/iframe-logs`. The desktop-client's
// subdomain proxy already forwards this origin to the correct mind with the
// session cookie attached, so we can use Electron's `net.fetch` (which shares
// the default session cookie jar) without additional auth plumbing.
//
// Back-pressure: each per-mind queue is bounded (`maxQueueSize`). On overflow
// we drop the oldest records rather than the newest, because the newest
// records are the most likely to correspond to the bug the user is actively
// debugging.
//
// Failure handling: if a POST fails, we drop that batch. Iframe logs are
// best-effort diagnostic output; retrying indefinitely would back up memory
// during a network outage.

'use strict';

const DEFAULT_FLUSH_AT_SIZE = 50;
const DEFAULT_MAX_QUEUE_SIZE = 1000;

/**
 * @typedef {Object} BufferRecord
 * @property {string} level
 * @property {string} message
 * @property {string} frame_url
 * @property {string} [source_id]
 * @property {number} [line]
 * @property {string} service_name
 * @property {string} mind_id
 * @property {string} [client_timestamp]
 */

/**
 * @typedef {Object} BufferOptions
 * @property {(url: string, init: object) => Promise<{ok: boolean, status: number}>} fetchFn
 * @property {number} [flushAtSize]
 * @property {number} [maxQueueSize]
 * @property {(err: Error, mindId: string) => void} [onError]
 */

class IframeLogBuffer {
  /**
   * @param {BufferOptions} options
   */
  constructor(options) {
    if (!options || typeof options.fetchFn !== 'function') {
      throw new Error('IframeLogBuffer requires a fetchFn');
    }
    this._fetchFn = options.fetchFn;
    this._flushAtSize = options.flushAtSize ?? DEFAULT_FLUSH_AT_SIZE;
    this._maxQueueSize = options.maxQueueSize ?? DEFAULT_MAX_QUEUE_SIZE;
    this._onError = options.onError ?? (() => {});
    /** @type {Map<string, {port: number, records: BufferRecord[]}>} */
    this._queues = new Map();
    this._closed = false;
  }

  /**
   * Add a record to the per-mind queue. Returns true if an auto-flush was
   * triggered by the size threshold (caller may await it, or ignore).
   *
   * @param {string} mindId
   * @param {number} port  desktop-client listening port
   * @param {BufferRecord} record
   * @returns {Promise<void>|null}
   */
  enqueue(mindId, port, record) {
    if (this._closed) return null;
    let entry = this._queues.get(mindId);
    if (entry === undefined) {
      entry = { port, records: [] };
      this._queues.set(mindId, entry);
    } else {
      // Port can change if the backend restarts; keep the most recent.
      entry.port = port;
    }
    entry.records.push(record);
    // Drop-oldest overflow: preserve the freshest records.
    while (entry.records.length > this._maxQueueSize) {
      entry.records.shift();
    }
    if (entry.records.length >= this._flushAtSize) {
      return this.flush(mindId);
    }
    return null;
  }

  /**
   * Flush the queue for a single mind, if any.
   *
   * @param {string} mindId
   */
  async flush(mindId) {
    const entry = this._queues.get(mindId);
    if (entry === undefined || entry.records.length === 0) return;
    const records = entry.records;
    entry.records = [];
    const url = `http://${mindId}.localhost:${entry.port}/api/iframe-logs`;
    try {
      const response = await this._fetchFn(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ records }),
      });
      if (!response.ok) {
        this._onError(
          new Error(`iframe-logs POST returned HTTP ${response.status}`),
          mindId,
        );
      }
    } catch (err) {
      this._onError(err instanceof Error ? err : new Error(String(err)), mindId);
    }
  }

  /**
   * Flush every queued mind. Called periodically by a caller-owned timer
   * and at shutdown.
   */
  async flushAll() {
    const mindIds = Array.from(this._queues.keys());
    await Promise.all(mindIds.map((mindId) => this.flush(mindId)));
  }

  /**
   * Stop accepting new records. Caller should also call flushAll() if it
   * wants to drain queued records before exit.
   */
  close() {
    this._closed = true;
  }

  /**
   * Test helper: snapshot of current queue sizes.
   * @returns {Record<string, number>}
   */
  _queueSizes() {
    const out = {};
    for (const [mindId, entry] of this._queues.entries()) {
      out[mindId] = entry.records.length;
    }
    return out;
  }
}

module.exports = { IframeLogBuffer, DEFAULT_FLUSH_AT_SIZE, DEFAULT_MAX_QUEUE_SIZE };
