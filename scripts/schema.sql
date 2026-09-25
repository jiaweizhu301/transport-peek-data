-- (a) 时刻表库：文件名 <mode>.sqlite（mode ∈ sydneytrains | metro | buses | lightrail | ferries | nswtrains），一文件一模式
PRAGMA page_size = 4096;            -- 只读分发库，管线生成后 VACUUM
PRAGMA journal_mode = DELETE;       -- 禁止 WAL（只读打开需可写 -shm 会失败）。客户端用 file:<path>?immutable=1 只读打开，免锁免 journal 探测
-- 分发：下载到 <mode>.sqlite.gz.tmp → gzip 解压到 <mode>.sqlite.tmp → sha256 校验 → rename 原子替换；旧连接持旧 inode 延迟 close（代际切换）
PRAGMA user_version = 2;            -- 与 meta.schema_version 同值，供客户端不读表即可校验；下表为单行元数据
CREATE TABLE meta (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version  INTEGER NOT NULL,                -- 本 DDL 版本，当前 2
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
-- 附近站（4.11）：按坐标框选父站。先用 (lat, lon) 的复合索引做一维区间 + 二维过滤，
-- 不引 R-Tree —— R-Tree 是 SQLite 的可选模块，Android 自带的 SQLite 不保证编进去，
-- 而 4.7 的 FTS5 已经有一条「按设备/厂商有差异」的同类风险，不再叠第二个。
-- 公交 3.7 万站下这条索引的代价实测见 4.3 关账。
CREATE INDEX idx_stops_latlon ON stops(lat, lon) WHERE location_type = 1;
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
-- 班次：一个 trip = 一趟车。
-- **schema 2 起停站时刻不再逐趟存表**，而是 pattern（停站几何，跨趟共享）+ 每趟一串 offsets。
-- 为什么不把时刻并进 pattern 键：实测公交把时刻偏移塞进 pattern 键之后压缩比从 9.25× 掉到
-- 2.32×（buses 单模式 80 MB），拆成两层是 35.7 MB。火车 50.6 → 7.2 MB。
CREATE TABLE trips (
  trip_id             TEXT PRIMARY KEY,
  route_id            TEXT NOT NULL REFERENCES routes(route_id),
  service_id          TEXT NOT NULL,
  headsign            TEXT,                        -- 原始 trip_headsign（终点显示用）
  direction_id        INTEGER,                     -- 原样保留；F4 公交线路按它分方向（M6）
  direction_group_id  INTEGER NOT NULL REFERENCES direction_groups(id),
  pattern_id          INTEGER NOT NULL REFERENCES stop_patterns(pattern_id),
  start_secs          INTEGER NOT NULL,            -- 首站 departure 的运营日秒数（本趟所有时刻的基准）
  duration_secs       INTEGER NOT NULL,            -- 末站 departure − start_secs
  offsets             BLOB    NOT NULL             -- 见下「offsets 的字节契约」
) WITHOUT ROWID;
CREATE INDEX idx_trips_route_service ON trips(route_id, service_id);
-- 出发查询的预筛索引：本趟任一站的 departure ∈ [start_secs, start_secs + duration_secs]，
-- 所以 SQL 能先按 (pattern_id, start_secs) 把候选趟砍到窗口内，只对幸存者解 blob。
-- 实测全网最忙的公交站：候选趟 1,503，套上 2 小时窗口只剩 334。
CREATE INDEX idx_trips_pattern ON trips(pattern_id, start_secs);

-- ── offsets 的字节契约（Kotlin 与 Python 两侧必须逐字节一致，改它就是改 schema）──
-- 按 stop_sequence 升序，每站两个值，各自是 zigzag 编码后的 unsigned LEB128 varint：
--   第 1 个 = 本站 arrival  − 上一站 departure   （站间运行时间；首站的「上一站 departure」= start_secs）
--   第 2 个 = 本站 departure − 本站 arrival      （停站时长）
-- 于是逐站递推：arr = prev_dep + v1；dep = arr + v2；prev_dep = dep。
-- 两个值都是小的非负数（跨零点的 24:00+ 也仍是递增秒数），varint 基本 1 字节/值。
-- zigzag 是防线不是优化：上游偶有 arrival > departure 的脏数据，负数不能当 unsigned 编。
-- 不变量（build_db.py 落库前逐趟断言）：解出的站数 == stop_patterns.n_stops，
-- 且末站 departure − start_secs == duration_secs。
-- 停站时刻：schema 2 起**不再是表，而是 pattern_stops ⋈ trips 的 VIEW**。
--
-- ⚠ 它**没有** arrival_secs / departure_secs 两列 —— 时刻在 offsets blob 里，SQL 解不开 varint。
--   这是有意的：留两个假列（例如 start_secs + 某个近似）会让「时刻是对的」这件事静默出错。
--   任何还在 SELECT arrival_secs 的旧 SQL 会在 prepare 阶段就报 no such column，是**响亮**地坏掉。
--   时刻的过滤与排序改在 Kotlin 侧解完 blob 之后做（契约 2 §2.4）。
-- 几何那半（stop_id / stop_sequence / pickup_type / drop_off_type）与 schema 1 逐字相同，
-- 所以只用到几何的查询（方向组聚合、「是否经过某站」的 EXISTS 子查询）一个字都不用改。
CREATE VIEW stop_times AS
  SELECT t.trip_id       AS trip_id,
         ps.stop_sequence AS stop_sequence,
         ps.stop_id       AS stop_id,
         ps.pickup_type   AS pickup_type,
         ps.drop_off_type AS drop_off_type,
         t.pattern_id     AS pattern_id,
         t.start_secs     AS start_secs,
         t.duration_secs  AS duration_secs,
         t.offsets        AS offsets
  FROM pattern_stops ps JOIN trips t ON t.pattern_id = ps.pattern_id;
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
-- 停站模式：**只按停站几何去重**（route_id + (stop_sequence, stop_id, pickup_type, drop_off_type) 序列），
-- 不含时刻。时刻在 trips.offsets 里。实测复用度：火车 29,018 趟 → 997 个 pattern，
-- 公交 90,546 趟 → 10,761 个。schema 1 的 stop_ids JSON 列去掉了 —— 热路径要的是可索引的行，
-- 不是一个要在应用层解析的字符串。
CREATE TABLE stop_patterns (
  pattern_id INTEGER PRIMARY KEY,
  route_id   TEXT    NOT NULL REFERENCES routes(route_id),
  n_stops    INTEGER NOT NULL                      -- = 该 pattern 的 pattern_stops 行数，供 blob 解码自检
);
CREATE TABLE pattern_stops (
  pattern_id     INTEGER NOT NULL REFERENCES stop_patterns(pattern_id),
  stop_sequence  INTEGER NOT NULL,                 -- 原样保留上游值（不重排），与 offsets 的顺序一致
  stop_id        TEXT    NOT NULL REFERENCES stops(stop_id),
  pickup_type    INTEGER NOT NULL DEFAULT 0,       -- 1 = 不上客（终点站）；出发列表需排除
  drop_off_type  INTEGER NOT NULL DEFAULT 0,       -- 1 = 不下客（始发站）；Trip 终点查询需排除
  PRIMARY KEY (pattern_id, stop_sequence)
) WITHOUT ROWID;
-- 出发查询的入口：站 → 经过它的 pattern。时刻不在这里，所以索引里没有 departure。
CREATE INDEX idx_pattern_stops_stop ON pattern_stops(stop_id);
-- 运营日约定（写在契约里供 core-gtfs 与管线共用）：运营日 D 的秒数 s 对应绝对时刻 = D 的悉尼本地正午 − 12h + s
-- （GTFS 标准「noon minus 12h」规则，DST 切换日仍成立）。客户端判定「当前运营日」：本地 04:00 之前归前一运营日，需同时查 D 与 D−1。

