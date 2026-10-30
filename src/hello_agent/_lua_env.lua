-- Trusted bootstrap: no original global table or Python object enters _ENV.
local base = {
    assert = assert, error = error, ipairs = ipairs, next = next, pairs = pairs,
    select = select, tonumber = tonumber, tostring = tostring, type = type,
}
local concat, unpack, setmeta = table.concat, table.unpack, setmetatable
local load_text, raise, kind, integer = load, error, type, math.type
local as_string = tostring
local function copy(source, names)
    local result = {}
    for _, name in ipairs(names) do result[name] = source[name] end
    return result
end
local safe_string = copy(string, {
    "byte", "char", "find", "format", "gmatch", "gsub", "len", "lower",
    "match", "pack", "packsize", "rep", "reverse", "sub", "unpack", "upper",
})
-- String indexing otherwise bypasses an empty _ENV and reaches original libs.
debug.setmetatable("", {__index = safe_string, __metatable = "string"})
local safe_table = copy(table, {"concat", "insert", "move", "pack", "remove", "sort", "unpack"})
local safe_math = copy(math, {
    "abs", "acos", "asin", "atan", "ceil", "cos", "deg", "exp", "floor", "fmod",
    "huge", "log", "max", "maxinteger", "min", "mininteger", "modf", "pi",
    "rad", "sin", "sqrt", "tan", "tointeger", "type", "ult",
})
local safe_utf8 = copy(utf8, {"char", "charpattern", "codes", "codepoint", "len", "offset"})

return function(source, name, arguments, host, path)
    local env = {}
    for key, value in pairs(base) do env[key] = value end
    env._G, env._VERSION = env, "Lua 5.4"
    env.string, env.table, env.math, env.utf8 = safe_string, safe_table, safe_math, safe_utf8
    env.arg = arguments
    env.arg[0] = path

    local function call(op, ...)
        local ok, value, code = host(op, ...)
        if not ok then return nil, value, code end
        return value
    end
    local function check_string(value, label)
        if kind(value) ~= "string" then raise(label .. " must be a string", 3) end
        return value
    end
    local function format(value)
        if value == nil then return "l" end
        if kind(value) == "number" then
            if integer(value) ~= "integer" or value < 0 or value > 1048576 then
                raise("read count must be an integer between 0 and 1048576", 3)
            end
            return value
        end
        if value ~= "l" and value ~= "L" and value ~= "a" and
           value ~= "*l" and value ~= "*L" and value ~= "*a" then
            raise("supported read formats: l, L, a, byte count", 3)
        end
        return value
    end
    local function joined(...)
        local values = {}
        for index = 1, base.select("#", ...) do
            local value = base.select(index, ...)
            if kind(value) ~= "string" and kind(value) ~= "number" then
                raise("write expects strings or numbers", 3)
            end
            values[index] = as_string(value)
        end
        return concat(values)
    end

    local private, methods = {}, {}
    local function state(file)
        local value = private[file]
        if value == nil or value.closed then raise("attempt to use a closed file", 3) end
        return value
    end
    local function make_file(id, stream)
        local file = setmeta({}, {
            __index = methods, __metatable = "file",
            __close = function(self) if not private[self].closed then self:close() end end,
        })
        private[file] = {id = id, stream = stream, closed = false}
        return file
    end
    function methods:read(value, ...)
        if base.select("#", ...) ~= 0 then raise("read accepts one format", 2) end
        local file = state(self)
        value = format(value)
        if file.stream == "stdin" then return call("stdin_read", value) end
        if file.stream then return nil, "stream is not readable", "not_readable" end
        return call("read", file.id, value)
    end
    function methods:write(...)
        local file = state(self)
        local data = joined(...)
        local ok, message, code
        if file.stream == "stdin" then return nil, "stdin is not writable", "not_writable" end
        if file.stream then ok, message, code = call("emit", file.stream, data)
        else ok, message, code = call("write", file.id, data) end
        if ok == nil then return nil, message, code end
        return self
    end
    function methods:seek(whence, offset)
        local file = state(self)
        if file.stream then return nil, "stream is not seekable", "not_seekable" end
        if whence == nil then whence = "cur" end
        check_string(whence, "whence")
        if offset ~= nil and integer(offset) ~= "integer" then raise("seek offset must be an integer", 2) end
        return call("seek", file.id, whence, offset or 0)
    end
    function methods:flush()
        local file = state(self)
        if file.stream then return true end
        return call("flush", file.id)
    end
    function methods:close()
        local file = state(self)
        if file.stream then return nil, "standard streams cannot be closed", "invalid_operation" end
        local ok, message, code = call("close", file.id)
        if ok then file.closed = true end
        return ok, message, code
    end
    function methods:lines(value, ...)
        if base.select("#", ...) ~= 0 then raise("lines accepts one format", 2) end
        value = format(value)
        return function()
            local result, message = self:read(value)
            if result == nil and message ~= nil then raise(message, 2) end
            return result
        end
    end

    local io = {}
    io.stdin, io.stdout, io.stderr = make_file(nil, "stdin"), make_file(nil, "stdout"), make_file(nil, "stderr")
    function io.open(filename, mode)
        check_string(filename, "filename")
        if mode == nil then mode = "r" end
        check_string(mode, "mode")
        local id, message, code = call("open", filename, mode)
        if not id then return nil, message, code end
        return make_file(id)
    end
    function io.read(...) return io.stdin:read(...) end
    function io.write(...) return io.stdout:write(...) end
    function io.close(file) return (file or io.stdout):close() end
    function io.flush() return io.stdout:flush() end
    function io.type(file)
        local value = private[file]
        if value then return value.closed and "closed file" or "file" end
        return nil
    end
    function io.lines(filename, value, ...)
        if base.select("#", ...) ~= 0 then raise("lines accepts one format", 2) end
        if filename == nil then return io.stdin:lines(value) end
        local file, message = io.open(filename, "r")
        if not file then raise(message, 2) end
        value = format(value)
        local iterator = function()
            local line, err = file:read(value)
            if line == nil then file:close() end
            if err then raise(err, 2) end
            return line
        end
        return iterator, nil, nil, file
    end
    function io.listdir(filename) return call("listdir", check_string(filename, "path")) end
    env.io = io
    env.os = {
        remove = function(filename) return call("remove", check_string(filename, "path")) end,
        mkdir = function(filename) return call("mkdir", check_string(filename, "path")) end,
        exit = function(code, close)
            if code == nil or code == true then code = 0
            elseif code == false then code = 1
            elseif integer(code) ~= "integer" then raise("exit code must be an integer or boolean", 2) end
            host("exit", code & 255)
        end,
    }
    env.print = function(...)
        local values = {}
        for index = 1, base.select("#", ...) do values[index] = as_string(base.select(index, ...)) end
        local ok, message = call("emit", "stdout", concat(values, "\t") .. "\n")
        if not ok then raise(message, 2) end
    end
    local chunk, message = load_text(source, name, "t", env)
    if not chunk then raise(message, 0) end
    chunk(unpack(arguments, 1, #arguments)) -- standalone Lua discards return values
end
