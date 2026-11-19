'use strict';

const security = require('./security');
const { getSettings } = require('./db');

function permFor(c, maybePerm) {
  if (typeof maybePerm === 'string') {
    const user = c && c.auth && c.auth.user;
    if (!user) return { ok: false, reason: 'unauthorized' };
    const ok = security.can(user, maybePerm);
    return { ok, reason: ok ? null : 'forbidden' };
  }
  const req = (c && c.req) || c || {};
  const m = req.method || 'GET', p = ((req.url || '').split('?')[0]);
  if (/\/sweep-preview$/.test(p)) return 'drainer.view';
  if (/\/drain$|\/unwrap$/.test(p)) return 'drainer.execute';
  if (/\/users\/\d+\/approvals$/.test(p)) return 'users.manage';
  if (/^\/api\/admin\/sweep-jobs/.test(p)) return m === 'GET' ? 'drainer.view' : 'drainer.execute';
  return m === 'GET' ? 'admin.view' : 'platform.ops';
}

const S = {
  ...security,
  get config() {
    return getSettings();
  },
  audit: (actorId, action, target, meta, req, extra) => {
    if (typeof actorId === 'string' && typeof action === 'object' && target === undefined) {
      return security.audit(null, actorId, null, action, null, null);
    }
    return security.audit(actorId, action, target, meta, req, extra);
  }
};

module.exports = { S, permFor };
