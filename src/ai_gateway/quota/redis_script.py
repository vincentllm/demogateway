"""Atomic Redis quota operations (stdlib-only module for wheel packaging)."""

SCRIPT = r'''
-- A single HSET commits account and lease together. Lua errors do NOT roll back
-- earlier writes, so there must be no separate mutating command for the ledger.
local op, client, request = ARGV[1], ARGV[2], ARGV[3]
local data = cjson.decode(ARGV[4])
local MAX = 9007199254740991
local function reply(value) return cjson.encode(value) end
local field = 'lease:' .. ARGV[5]
local saved = redis.call('HGET', KEYS[1], field)
if op == 'admit' then
    if saved then return reply({reason='duplicate_request'}) end
    local limits = data.limits
    for k, v in pairs(limits) do limits[k] = tonumber(v) end
    local now_parts = redis.call('TIME')
    local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
    local window = math.floor(now / limits.window_seconds)
    local old = redis.call('HMGET', KEYS[1], 'window', 'rpm', 'tokens', 'total',
                           'policy', 'overflow')
    local policy = table.concat({string.format('%.0f', limits.rpm),
        string.format('%.0f', limits.tpm), string.format('%.0f', limits.total_tokens),
        string.format('%.0f', limits.window_seconds)}, ':')
    if old[5] and old[5] ~= policy then return reply({reason='policy_mismatch'}) end
    if old[6] == '1' then return reply({reason='account_overflow'}) end
    local old_window = tonumber(old[1]) or window
    window = math.max(window, old_window)
    local rpm, tokens, total = 0, 0, tonumber(old[4]) or 0
    if old_window == window then
        rpm, tokens = tonumber(old[2]) or 0, tonumber(old[3]) or 0
    end
    local amount = tonumber(data.reserved_tokens)
    local retry = math.max(1, math.ceil((window + 1) * limits.window_seconds - now))
    if amount > limits.tpm then return reply({reason='reservation_too_large'}) end
    -- Subtraction avoids rounding an over-limit integer sum down at 2^53.
    if amount > limits.total_tokens - total then return reply({reason='total_tokens'}) end
    if rpm >= limits.rpm then return reply({reason='rpm', retry_after=retry}) end
    if amount > limits.tpm - tokens then return reply({reason='tpm', retry_after=retry}) end
    local lease = {client_id=client, request_id=request, lease_id=data.lease_id,
        input_bound=data.input_bound, max_output_tokens=data.max_output_tokens,
        reserved_tokens=data.reserved_tokens, window=string.format('%.0f', window)}
    local record = reply({lease=lease, state='pending'})
    local response = reply({lease=lease})
    -- Exactly one write. ACL/OOM/type failure cannot leave a pending lease
    -- paired with a refunded balance; no command or encoding follows mutation.
    redis.call('HSET', KEYS[1], 'window', string.format('%.0f', window),
        'rpm', string.format('%.0f', rpm + 1), 'tokens', string.format('%.0f', tokens + amount),
        'total', string.format('%.0f', total + amount), 'policy', policy, field, record)
    return response
end
if not saved then return reply({reason='invalid_lease'}) end
local record = cjson.decode(saved)
for k, v in pairs(record.lease) do
    if data.lease[k] ~= v then return reply({reason='invalid_lease'}) end
end
for k, v in pairs(data.lease) do
    if record.lease[k] ~= v then return reply({reason='invalid_lease'}) end
end
if record.state ~= 'pending' then return reply({changed=false}) end
local old = redis.call('HMGET', KEYS[1], 'window', 'tokens', 'total')
if not old[3] then return redis.error_reply('quota account missing') end
local lease = record.lease
local amount = data.total
if amount == cjson.null then amount = lease.reserved_tokens end
local delta = tonumber(amount) - tonumber(lease.reserved_tokens)
local total, tokens = tonumber(old[3]), tonumber(old[2])
local overflow = delta > 0 and (delta > MAX - total or
    (tonumber(old[1]) == tonumber(lease.window) and delta > MAX - tokens))
record.state = data.state
local encoded = reply(record)
local response = reply({changed=true})
local updates = {'total', string.format('%.0f', math.min(MAX, total + delta)), field, encoded}
if overflow then
    table.insert(updates, 'overflow')
    table.insert(updates, '1')
end
if tonumber(old[1]) == tonumber(lease.window) then
    table.insert(updates, 'tokens')
    table.insert(updates, string.format('%.0f', math.min(MAX, tokens + delta)))
end
redis.call('HSET', KEYS[1], unpack(updates))
return response
'''
