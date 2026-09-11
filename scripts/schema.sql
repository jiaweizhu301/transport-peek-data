-- (a) 时刻表库：文件名 <mode>.sqlite（mode ∈ sydneytrains | metro | buses | lightrail | ferries | nswtrains），一文件一模式
PRAGMA page_size = 4096;            -- 只读分发库，管线生成后 VACUUM
PRAGMA journal_mode = DELETE;       -- 禁止 WAL（只读打开需可写 -shm 会失败）。客户端用 file:<path>?immutable=1 只读打开，免锁免 journal 探测
-- 分发：下载到 <mode>.sqlite.gz.tmp → gzip 解压到 <mode>.sqlite.tmp → sha256 校验 → rename 原子替换；旧连接持旧 inode 延迟 close（代际切换）
PRAGMA user_version = 1;            -- 与 meta.schema_version 同值，供客户端不读表即可校验；下表为单行元数据
CREATE TABLE meta (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version  INTEGER NOT NULL,                -- 本 DDL 版本，当前 1
  mode            TEXT    NOT NULL,                -- 与文件名一致
  static_version  TEXT    NOT NULL,                -- 上游 GTFS 版本标识（feed_info 或 zip 的 Last-Modified，格式 YYYYMMDD[-hhmm]）
  calendar_start  INTEGER NOT NULL,                -- YYYYMMDD，calendar/calendar_dates 覆盖起点
  calendar_end    INTEGER NOT NULL,                -- YYYYMMDD，覆盖终点；客户端超过此日显示「时刻表待更新」
  generated_at    TEXT    NOT NULL                 -- ISO-8601 UTC，管线生成时间
);
-- 站点：父站（location_type=1）与站台子站（location_type=0 且 parent_station 非空）共存；收藏建在父站上
CREATE TABLE stops (
  stop_id          TEXT PRIMARY KEY,
  name             TEXT NOT NULL,                  -- 英文原名，不翻译
  name_normalized  TEXT NOT NULL,                  -- 小写、去标点/多余空白、去 "Station" 后缀，供 LIKE 'x%' 前缀搜索
  lat              REAL NOT NULL,
  lon              REAL NOT NULL,
  location_type    INTEGER NOT NULL DEFAULT 0,     -- 0 站台/站点 1 父站 2 出入口（出入口可裁掉）
  parent_station   TEXT REFERENCES stops(stop_id), -- 子站指向父站；父站为 NULL
  platform_code    TEXT                            -- 站台号，如 "16"；RT 换站台时以 stop_time_update.stop_id 反查此列
) WITHOUT ROWID;
CREATE INDEX idx_stops_parent ON stops(parent_station);
CREATE INDEX idx_stops_search ON stops(name_normalized) WHERE location_type = 1;   -- 搜索只命中父站
-- 线路：官方线路色程序绘制徽标
CREATE TABLE routes (
  route_id          TEXT PRIMARY KEY,
  agency_id         TEXT NOT NULL,                 -- 如 SydneyTrains / SMNW（Metro）
  route_short_name  TEXT NOT NULL,                 -- 如 T1 / M1
  route_long_name   TEXT,
  route_type        INTEGER NOT NULL,              -- GTFS route_type（2 铁路 / 1 地铁 / 3 公交 / 0 轻轨 / 4 渡轮）
  route_color       TEXT,                          -- 6 位 HEX，无 #
  route_text_color  TEXT
) WITHOUT ROWID;
-- 目的地方向组：同一线路按 trip_headsign 聚类；替代不可靠的 direction_id（环线）。
-- id 每日重排，**不得被客户端持久化**；跨版本稳定键 = direction_key（收藏存它，运行时反查 id）
CREATE TABLE direction_groups (
  id                INTEGER PRIMARY KEY,
  route_id          TEXT NOT NULL REFERENCES routes(route_id),
  direction_key     TEXT NOT NULL,                 -- 稳定键 = 归一化目的地（小写、去 "via …"、去空白），如 "hornsby"；跨线路同目的地同 key
  headsign_pattern  TEXT NOT NULL,                 -- 管线聚类依据（可含 via）
  label             TEXT NOT NULL,                 -- 展示文案（英文原名，如 "Hornsby via Strathfield"）
  UNIQUE (route_id, headsign_pattern)
);
CREATE INDEX idx_direction_groups_key ON direction_groups(direction_key);   -- 跨线路合并显示（M2 DP）
-- 班次：一个 trip = 一趟车；pattern_id 为 Buses 阶段 pattern 归一化预留，M1 恒为 NULL
CREATE TABLE trips (
  trip_id             TEXT PRIMARY KEY,
  route_id            TEXT NOT NULL REFERENCES routes(route_id),
  service_id          TEXT NOT NULL,
  headsign            TEXT,                        -- 原始 trip_headsign（终点显示用）
  direction_id        INTEGER,                     -- 原样保留，仅供调试，不用于业务
  direction_group_id  INTEGER NOT NULL REFERENCES direction_groups(id),
  pattern_id          INTEGER REFERENCES stop_patterns(pattern_id)   -- 预留，Buses 时启用
) WITHOUT ROWID;
CREATE INDEX idx_trips_route_service ON trips(route_id, service_id);
-- 停站时刻：arrival/departure 为运营日秒数；出发查询走 (stop_id, departure_secs)，行程展开走 (trip_id, stop_sequence)
CREATE TABLE stop_times (
  trip_id         TEXT    NOT NULL REFERENCES trips(trip_id),
  stop_sequence   INTEGER NOT NULL,
  stop_id         TEXT    NOT NULL REFERENCES stops(stop_id),   -- 子站/站台 id（火车、Metro）；客户端聚合到父站
  arrival_secs    INTEGER NOT NULL,
  departure_secs  INTEGER NOT NULL,
  pickup_type     INTEGER NOT NULL DEFAULT 0,      -- 1 = 不上客（终点站）；出发列表需排除
  drop_off_type   INTEGER NOT NULL DEFAULT 0,      -- 1 = 不下客（始发站）；Trip 终点查询需排除
  PRIMARY KEY (trip_id, stop_sequence)
) WITHOUT ROWID;
CREATE INDEX idx_stop_times_stop_dep ON stop_times(stop_id, departure_secs);
-- 服务日历：周几运行 + 起止日期（YYYYMMDD 整数）
CREATE TABLE calendar (
  service_id  TEXT PRIMARY KEY,
  monday INTEGER NOT NULL, tuesday INTEGER NOT NULL, wednesday INTEGER NOT NULL, thursday INTEGER NOT NULL,
  friday INTEGER NOT NULL, saturday INTEGER NOT NULL, sunday INTEGER NOT NULL,
  start_date INTEGER NOT NULL, end_date INTEGER NOT NULL
) WITHOUT ROWID;
-- 日历例外：exception_type 1 = 增加运行日，2 = 取消运行日（公众假日、trackwork）
CREATE TABLE calendar_dates (
  service_id TEXT NOT NULL, date INTEGER NOT NULL, exception_type INTEGER NOT NULL,   -- date = YYYYMMDD
  PRIMARY KEY (service_id, date)
) WITHOUT ROWID;
-- 换乘：M1 只搬运不使用，M7 行程规划启用
CREATE TABLE transfers (
  from_stop_id TEXT NOT NULL REFERENCES stops(stop_id), to_stop_id TEXT NOT NULL REFERENCES stops(stop_id),
  transfer_type INTEGER NOT NULL DEFAULT 0, min_transfer_time INTEGER,   -- 秒
  PRIMARY KEY (from_stop_id, to_stop_id)
) WITHOUT ROWID;
-- 停站模式预留表：Buses 加入时把重复的 stop 序列归一化到此表，trips.pattern_id 引用。M1 建表但为空
CREATE TABLE stop_patterns (
  pattern_id INTEGER PRIMARY KEY, route_id TEXT NOT NULL REFERENCES routes(route_id),
  stop_ids TEXT NOT NULL                           -- JSON 数组，按 stop_sequence 排列
);
-- 运营日约定（写在契约里供 core-gtfs 与管线共用）：运营日 D 的秒数 s 对应绝对时刻 = D 的悉尼本地正午 − 12h + s
-- （GTFS 标准「noon minus 12h」规则，DST 切换日仍成立）。客户端判定「当前运营日」：本地 04:00 之前归前一运营日，需同时查 D 与 D−1。

