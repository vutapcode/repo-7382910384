# WStrade Architecture Recovery Series — kế hoạch tạm

> Trạng thái: **ACTIVE CHECKLIST — Đợt 0 PARTIAL/BLOCKED do thiếu canonical strategy replay; Đợt 1 đã triển khai, còn authenticated verification**
> Phạm vi của lần tạo file này: chỉ tổng hợp hai báo cáo kiến trúc thành chuỗi fix có điểm dừng.  
> Khi khởi tạo file chưa sửa code/restart/push; từ Đợt 1 mọi thay đổi phải theo mutation loop và Mainnet vẫn khóa.  
> Xóa file này sau khi toàn bộ đợt đã PASS và tài liệu nguồn chính thức đã được cập nhật.

## 1. Quyết định kiến trúc

North Star:

```text
RAW EVENTS
  -> MARKET TRUTH
  -> ACTION POLICY
  -> EXECUTION POLICY
  -> EXCHANGE

HARD RISK / SRE có quyền BLOCK, HALT, REDUCE, EXIT
nhưng không được viết lại MARKET TRUTH.
```

Các invariant bắt buộc:

```text
BELIEF != ACTION != EXECUTION != SAFETY
UNKNOWN != CONTRADICTED != FALSIFIED
PROCESS_UP != DATA_RECONCILED != STRATEGY_WARM != ENTRY_READY
UNPROTECTED_EXPOSURE != PROTECTED_POSITION
```

Winner kiến trúc:

- Một owner duy nhất trả lời **thị trường đang xảy ra cơ chế gì**.
- Action chỉ chọn `ACT_TAKER / POST_MAKER / WAIT_INFORMATION / ABANDON` từ thesis bất biến.
- Execution chỉ kiểm tra intent còn thực thi được và chưa gặp contradiction vật lý.
- Guardian theo dõi cùng thesis; không dựng một market truth thứ hai.
- Hard Risk/SRE có quyền tối cao về an toàn, nhưng lý do thoát do mất quan sát phải là `SYSTEM_UNSAFE`, không được giả thành `THESIS_FALSE`.
- Brain mới chỉ được **replace** brain cũ sau bằng chứng replay/shadow; tuyệt đối không ensemble thành score soup.

## 2. Mức độ tin cậy của hai báo cáo

Hai báo cáo là đầu vào điều tra, **không phải đặc tả production đã được chứng minh**.

### Giữ làm giả thuyết P0 cần kiểm chứng bằng code/runtime

- Guardian có đang tái dựng truth bằng S1/S2/S3 thay vì consume thesis hay không.
- `UNKNOWN` có đang bị ghi hoặc downstream hiểu như `FALSIFIED/BROKEN` hay không.
- Entry có tái-phán cùng causal proof của Ignition hay không.
- Có cửa sổ `FILL -> stop chưa VERIFIED` dài bao nhiêu và failure path thật là gì.
- REST timeout/retry có làm order tới sàn sau khi thesis stale hay không.
- Pseudo-confidence, regime multiplier và PnL path có authority thật ở launcher active hay chỉ là metadata.
- Những hook nào thật sự đổi behavior; file tồn tại không đồng nghĩa active authority.

### Semantics nguồn Binance đã kiểm chứng khi lập kế hoạch

- USD-M order-book snapshot trả riêng `E` (message output), `T` (transaction time) và `lastUpdateId`.
- Recent trades trả trade time, trade identity và maker-side semantics.
- OI endpoint trả một snapshot `openInterest + time`; một điểm OI không đủ chứng minh ai build/unwind.
- Do đó phải giữ riêng `event_time / receive_time / available_time / sequence / epoch`; không được dùng một timestamp chung giả causal ordering.
- Order-book snapshot không tự chứng minh executed depletion, refill hay absorption; phải ghép update sequence với trades và price conversion.
- Plugin market-data không chứng minh authenticated order ACK, private-stream lag, stop placement hay secondary trading transport. Các mục đó bắt buộc test ở môi trường authenticated an toàn.

### Không được triển khai mù quáng

- Không xóa toàn bộ Bias/Guardian/threshold chỉ vì báo cáo gọi chúng là legacy.
- Không tạo `MarketTruthEngine` mới nếu repo đã có canonical world/thesis model tương đương; phải mở rộng owner hiện hữu.
- Không mặc định Binance WebSocket API là fallback order transport cho tới khi xác minh capability, idempotency và failure-domain.
- Không dựng warm standby/HA hoặc phát sinh chi phí AWS mới khi chưa có phê duyệt riêng.
- Không bỏ CPU governor. CPU `<30%` trên cửa sổ 15 phút và 1 giờ vẫn là capacity invariant; chỉ không được coi nó là latency/data-integrity SLO.
- Không bật trade-count/loss-streak cap cho demo. Safety vốn chỉ áp dụng tiền thật theo config tách biệt.

## 3. Luật thực thi chung cho mọi đợt

- [ ] Mainnet bị khóa; mutation strategy chỉ chạy demo/shadow.
- [ ] Lần authority từ launcher active trước khi sửa; không suy từ tên file.
- [ ] Mỗi mutation phục vụ đúng một câu hỏi causal/operational.
- [ ] Mỗi đợt có baseline commit, config hash, WAL/schema version và rollback commit.
- [ ] Live và replay dùng cùng `available_time`; cấm lookahead.
- [ ] Test trên cùng WAL, frozen costs, current Guardian và cùng causal-wave matching.
- [ ] Không tune threshold để ép test PASS.
- [ ] Không trộn cohort/schema cũ với semantics mới.
- [ ] Sau test: restart an toàn -> thu runtime -> kiểm tra state/log -> chỉ push khi runtime đúng.
- [ ] Toàn host trung bình `<30%` trên mọi cửa sổ 15 phút và 1 giờ; target vận hành nội bộ giữ vùng đệm.
- [ ] Nếu một PASS phụ thuộc authenticated exchange behavior chưa test được: ghi `UNVERIFIED_AUTHENTICATED_PATH`, không giả PASS.

## 4. Chuỗi triển khai

---

# Đợt 0 — Evidence freeze và authority map

## Câu hỏi

Các nhận định nào thực sự nằm trên active path, module nào đang sở hữu câu hỏi nào, và baseline nào sẽ dùng để chứng minh bản sửa tốt hơn?

## Công việc

- [x] Ghim commit/config/runtime mode hiện tại; xác nhận Mainnet khóa.
- [x] Vẽ active call graph: source -> normalize -> state -> truth -> action -> execution -> Guardian -> Hard Risk -> journal.
- [x] Lập bảng `QUESTION -> OWNER -> INPUT -> OUTPUT -> CONSUMERS -> WRONG-ANSWER IMPACT`.
- [x] Liệt kê mọi behavior-changing hook và research-only subscriber.
- [x] Search authority thật của confidence, regime multiplier, lead label, OI label, Entry revalidation, Guardian S1/S2/S3.
- [ ] Chụp baseline test/replay hash, opportunity count, trades, misses, capture ratio, latency và CPU. Runtime/WAL transport đã khóa; canonical strategy replay và post-audit CPU cooldown còn thiếu.
- [x] Khóa một WAL version-bounded sạch; không reset/xóa dữ liệu nguồn. Range và hai transport hash nằm trong Phase 0 artifact.

## PASS

- [x] Mỗi câu hỏi quan trọng có đúng một owner được ghi rõ trong `artifacts/architecture_recovery/PHASE0_EVIDENCE_FREEZE.md`.
- [x] Không còn nhận định “active” chỉ dựa vào file tồn tại; map lần từ canonical launcher và wrappers.
- [ ] Baseline replay deterministic hai lần cùng hash.

## STOP/FAIL

- [x] Không tái tạo được full canonical strategy baseline: giữ stop gate cho mọi strategy mutation tiếp theo và sửa replay adapter trước. Đợt 1 đã hoàn thành out-of-order chỉ vì đó là safety/execution transaction, không phải thay đổi market reasoning.

### Kết quả hồi tố 2026-09-02

- Đợt 1 đã được triển khai trước khi evidence freeze Đợt 0 hoàn tất; baseline
  trước Đợt 1 được ghim ở commit `90a04c29ae29766df45a14b528119cbf1325cfc8`.
- Authority map và question-owner contract đã được khóa ở
  `artifacts/architecture_recovery/PHASE0_EVIDENCE_FREEZE.md`.
- Full canonical Ignition replay adapter chưa tồn tại. Cho tới khi adapter đó
  replay đủ Bias/Entry/Edge/fill/Guardian/Hard Risk, Đợt 0 mang trạng thái
  `BLOCKED_CANONICAL_ADAPTER_MISSING`; deterministic transport hash không được
  giả thành strategy replay PASS.

---

# Đợt 1 — Execution survival và control-plane observability

## Câu hỏi

Sau khi exchange fill, vốn có được bảo vệ chắc chắn không; và khi order-control plane chậm/hỏng, hệ thống biết điều đó trước khi gửi Entry mới không?

## 1A. Execution Protection Transaction

- [x] Chuẩn hóa state:

```text
INTENT_CREATED
-> ORDER_SENT
-> ACK_KNOWN | EXECUTION_UNKNOWN
-> FILL_CONFIRMED
-> UNPROTECTED_EXPOSURE
-> PROTECTION_SENT
-> PROTECTION_ACKNOWLEDGED
-> PROTECTION_VERIFIED
-> POSITION_PROTECTED
```

- [x] Phân biệt ACK, fill, stop ACK và stop VERIFIED; không suy từ local intent.
- [x] Ghi monotonic timestamps:
  - `decision_to_submit_ms`
  - `submit_to_ack_ms`
  - `fill_to_protection_submit_ms`
  - `fill_to_protection_ack_ms`
  - `fill_to_protection_verified_ms`
  - `execution_unknown_duration_ms`
- [x] Emergency path chỉ xử lý exposure, không gọi lại strategy reasoning.
- [x] Idempotent reconciliation theo client/order/trade identity; chống duplicate protection/flatten.

## 1B. Execution Control Plane Health

- [ ] Đo REST RTT, order/cancel ACK, private-stream lag và exchange clock skew. REST RTT/order ACK/private-stream observed lag đã có; exchange clock skew riêng chưa được chứng minh nên chưa đánh dấu xong.
- [x] State health có nghĩa vật lý: `HEALTHY / DEGRADED / UNSAFE_FOR_NEW_ENTRY / EXIT_ONLY / UNKNOWN`.
- [x] Ngưỡng chỉ được đề xuất sau khi có distribution runtime; trước đó telemetry-only.
- [x] Pre-submit revalidation phải kiểm episode identity, data freshness và contradiction vật lý.
- [x] Không giảm REST timeout nếu chưa chứng minh request ambiguity/reconciliation an toàn.

## Tests bắt buộc

- [x] Fill xảy ra, stop submit timeout, query trả unknown.
- [x] Stop ACK nhưng chưa query-verified.
- [x] Duplicate user-stream fill/order update.
- [x] Process chết giữa fill và protection.
- [ ] Network lag khiến Entry intent stale trước ACK. Pre-submit stale đã có; post-submit ACK lag mới chỉ được đo, chưa thể hủy một order đã gửi.
- [x] Emergency flatten cũng timeout.
- [x] Restart khi exchange có position/order nhưng local state thiếu.

## PASS

- [x] Không state nào gọi position “protected” trước exchange verification.
- [x] New Entry bị khóa khi execution health unsafe; exit/reconciliation vẫn sống.
- [x] Không double order/stop/flatten trong synthetic chaos tests.
- [x] Authenticated path chưa test thì vẫn fail-closed và mang nhãn chưa xác minh.

### Kết quả triển khai 2026-09-02

- Transaction V1 được checkpoint trước submit, ngay sau fill, khi exposure chưa có stop, trước stop submit và sau exchange verification.
- Stop HTTP ACK không còn đủ quyền gọi position protected; bắt buộc query thấy đúng `clientAlgoId/algoId`.
- Emergency flatten dùng client ID bền và không submit lần hai khi kết quả cũ còn unknown; chỉ gọi FLAT sau independent position query.
- Control latency chưa có calibration nên p95 quá budget chỉ telemetry `DEGRADED`; transport failure/unknown và private-stream failure vẫn fail-closed.
- Unit/synthetic coverage hoàn tất; authenticated Binance order/stop path vẫn chưa được kiểm chứng vì connector plugin hiện chỉ cung cấp public market data.

## Điểm dừng

Không sang Đợt 2 nếu transaction state có impossible transition hoặc reconciliation không deterministic.

---

# Đợt 2 — Temporal truth và data-integrity authority

## Câu hỏi

Bot có biết chính xác dữ liệu nào đã thực sự available tại thời điểm quyết định, và causal ordering có lớn hơn sai số đo không?

## Công việc

- [x] Một canonical event contract cho live/recorder/replay:

```text
source
stream
event_id / sequence
exchange_event_time
receive_time_monotonic
available_time_monotonic
epoch
source_health
payload_version
```

- [x] `available_time` là temporal authority của replay decision.
- [x] Ghi offset, jitter, batching uncertainty theo từng venue/stream.
- [x] Chỉ kết luận `A_LEADS_B` khi observed ordering vượt tổng uncertainty bound.
- [x] Không đủ bound -> `SIMULTANEOUS_OR_UNRESOLVED`, không bịa leader.
- [x] Gap/reset/out-of-order tạo epoch mới; không nối flow/state qua epoch.
- [x] OI delta chỉ có nghĩa khi có hai snapshot đúng thứ tự trong causal window.
- [x] Depth state chỉ có nghĩa khi snapshot + diff sequence được reconcile; executed depletion cần trade evidence.
- [x] Chuẩn hóa một storage-health owner cho bot và recorder.

## Tests bắt buộc

- [x] Late arrival không sửa quyết định quá khứ.
- [x] Same exchange time nhưng different available time.
- [x] Clock uncertainty lớn hơn lead gap.
- [x] Sequence gap/reconnect/epoch reset.
- [x] Same OI snapshot không được gọi build/unwind.
- [x] Static wall/cancel không được gọi absorption/depletion.

## PASS

- [ ] Live/replay cùng event contract và cùng decision hash.
- [x] Mọi causal-lead output kèm measurement status/bound.
- [x] Source mất dữ liệu -> `UNKNOWN/UNSAFE`, không thành `THESIS_FALSE`.

### Kết quả triển khai 2026-09-02

- Recorder schema V7 và Ignition Signals V4 dùng chung temporal contract;
  Binance `T/E`, trade/update identity và local availability không còn bị gộp.
- OI dùng exchange transaction time; same snapshot, epoch recovery hoặc mẫu
  ngoài causal window đều trả `UNKNOWN`, không được giả thành build/unwind.
- Replay transport chạy theo availability và đọc ngược V6 an toàn. Canonical
  strategy replay adapter vẫn chưa tồn tại, vì vậy mục PASS "cùng decision
  hash" ở trên cố ý chưa đánh dấu; deterministic transport hash không được giả
  làm strategy decision hash.

---

# Đợt 3 — Authority separation và semantic cleanup

## Câu hỏi

Market truth, quyết định hành động, khả năng thực thi và safety có còn viết đè lẫn nhau không?

## Contract cần chuẩn hóa

### Market Truth owner

Trả lời:

```text
mechanism
status = SUPPORTED | DIVERGING | FALSIFIED | UNKNOWN
supporting_evidence
competing_explanations
falsifiers
expected_next_observations
source_health
causal_episode_id
```

### Action owner

Chỉ trả:

```text
ACT_TAKER_NOW | POST_MAKER | WAIT_INFORMATION | ABANDON
```

### Execution owner

Chỉ trả:

```text
EXECUTE | CANCEL | EXECUTION_UNKNOWN
```

### Safety owner

Chỉ trả action safety và lý do operational; không rewrite thesis.

## Công việc

- [x] Tạo/chuẩn hóa immutable contracts trước; chưa đổi authority.
- [x] Journal cả bốn lớp bằng cùng `causal_episode_id` nhưng field riêng.
- [x] Tách `UNKNOWN_SOURCE`, `UNKNOWN_MARKET`, `CONTRADICTED`, `FALSIFIED`, `SYSTEM_UNSAFE`.
- [x] Gắn compatibility reader cho journal cũ; không cho field cũ có authority mới.
- [x] Viết invariant tests cấm Safety/Execution sửa MarketThesis.
- [x] Xác định canonical owner hiện hữu; không tạo wrapper/module trùng chức năng.

## PASS

- [x] Một câu hỏi chỉ có một owner.
- [x] Post-mortem nói được “thesis còn đúng nhưng buộc thoát vì mất quan sát”.
- [x] Không thay đổi decision count ở bước contract-only.

### Kết quả triển khai 2026-09-02

- `FOUR_AUTHORITY_CONTRACTS_V1` chỉ đóng dấu snapshot của bốn owner hiện hữu;
  module contract không có quyền tạo Market Truth, Action, Execution hay Safety.
- `guardian_s_tier.thesis_status` cũ vẫn là observation tương thích phục vụ
  exit hiện tại, không phải sealed Market Truth authority. Việc thay Guardian
  brain bằng shared thesis thuộc riêng Đợt 4 và chưa được làm ở đây.
- Market Truth, Action, Execution và Safety cùng dùng một `causal_episode_id`;
  sửa nội dung sau handoff làm hash invalid thay vì âm thầm đổi kết luận.
- Journal/replay cũ chỉ tạo compatibility view
  `authority_eligible=false`; không suy ngược field cũ thành authority mới.
- Contract-only path giữ nguyên các quyết định GO/WAIT hiện hữu. Toàn bộ `691`
  tests PASS (`2` intentional skips); repository integrity PASS cho `255` file.
- Không đổi threshold, Entry/Guardian/Hard Risk semantics hoặc Mainnet lock.

---

# Đợt 4 — Shared thesis migration: Entry và Guardian dùng chung sự thật

## Câu hỏi

Sau Entry, Guardian có theo dõi/falsify chính thesis đã mở vị thế hay đang dựng lại một brain khác?

## 4A. Entry handoff

- [x] Freeze thesis version, episode identity, mechanism, evidence, falsifiers và expected observations khi Action phê duyệt.
- [x] Entry không tái-phán causal proof đã thuộc Truth owner.
- [x] Entry chỉ được reject vì action economics hoặc dependency contract không thỏa; reason phải đúng owner.
- [x] Không để top-level `GO` khi thesis/action vẫn `WAIT`.

### Kết quả 4A — 2026-09-03

- `ENTRY_THESIS_HANDOFF_V1` đóng dấu đúng Market Truth + Action đã duyệt;
  thiếu episode, sai side hoặc mutation đều fail-closed trước submit.
- Launcher bỏ post-Action Bias re-adjudication; current dependency chỉ còn do
  Execution owner kiểm ngay trước submit.
- Shadow/live chỉ thay Execution contract sau revalidation, không rebuild ba
  owner còn lại. Journal gắn reject về `ACTION / EXECUTION / SAFETY`.
- Chỉ hoàn tất 4/16 checklist Đợt 4. Guardian 4B, shadow migration và PASS
  vẫn để mở; chưa thay Guardian exit semantics hoặc Hard Risk.

## 4B. Guardian shared thesis

- [x] Guardian consume frozen thesis + subsequent canonical events.
- [x] Output tối thiểu: `SUPPORT / DIVERGENCE / CONTROL_TRANSFER / FALSIFY / UNKNOWN`.
- [x] Normal noise/pullback không tự thành falsification.
- [x] Missing/stale evidence có thể buộc safety exit nhưng thesis status phải là `UNKNOWN`.
- [x] PnL, best-R, runner state và capital preference không được sửa market truth.
- [x] Hard SL/reconciliation/feed-critical vẫn bypass Guardian reasoning.

## Migration an toàn

- [x] Chạy shared-thesis Guardian shadow song song chỉ để so quyết định.
- [x] Same thesis + same events phải cho deterministic trace.
- [ ] So premature exits, late exits, capture ratio, hard-stop rate trên cùng WAL.
- [x] Chỉ cut authority khi shadow thắng acceptance; không weighted ensemble.

## PASS

- [ ] Guardian không có causal council độc lập trả lời trùng Truth owner.
- [x] Safety exit không đầu độc label thesis/calibration.
- [ ] Không giảm Guardian capture ratio hoặc tăng hard-stop rate ngoài giới hạn đã phê duyệt.

### Kết quả 4B shadow implementation — 2026-09-03

- `MARKET_THESIS_OBSERVATION_V1` là owner duy nhất ánh xạ frozen Entry truth và
  canonical observation sang năm trạng thái thesis; dữ liệu vốn/PnL bị loại
  khỏi payload được hash.
- `GUARDIAN_SHARED_THESIS_SHADOW_V1` chạy song song, `authority=false`, không
  weighted ensemble và không thay đổi quyết định/Hard Risk hiện tại.
- `phase4_guardian_shadow_report.py` xác minh lại contract/hash/determinism trên
  cùng WAL, tách safety exit khỏi thesis labels và fail-closed cutover.
- Chưa có position dùng checkpoint 4A trong WAL, nên chưa thể đo executable
  counterfactual net/capture/hard-stop. Vì vậy legacy Guardian vẫn giữ action
  authority và hai PASS về cutover/capture còn mở; không được đánh dấu Đợt 4
  hoàn tất bằng synthetic test.

---

# Đợt 5 — Loại pseudo-authority, giữ empirical/physical meaning

## Câu hỏi

Con số nào đang thật sự đo một cơ chế, và con số nào chỉ tạo cảm giác chính xác?

## Shadow ablation từng biến, không xóa hàng loạt

- [ ] Guardian pseudo-confidence -> timing.
- [ ] `microstructure_regime` multiplier vào threshold/cost/expectancy.
- [ ] Displacement dominance bị gọi là causal lead.
- [ ] Bias weighted vote/pseudo-confidence có final authority.
- [ ] Dual venue bị gọi independent causal roots.
- [ ] OI expansion/contraction bị gọi identity build/unwind.
- [ ] Fixed slippage/min-net/TTL giả empirical.
- [ ] Duplicate Entry causal validators.
- [ ] Auto-promotion có quyền runtime hay chỉ evidence gate.

## Quy tắc quyết định

- Physical invariant: giữ hard.
- Measurement uncertainty: đo bound rồi mới cấp authority.
- Empirical behavior: replay/shadow với confidence interval và all-outcome data.
- Chưa đủ bằng chứng: `UNKNOWN/PRIOR_ONLY`, không có quyền quyết định.

## PASS cho mỗi removal

- [ ] One-variable ablation, cùng WAL/cost/Guardian.
- [ ] Không tăng economic miss hoặc false-entry rate material.
- [ ] Không có consumer ẩn còn đọc field retired.
- [ ] Docs/tests ghi rõ semantic mới.
- [ ] Một concern = một rollback độc lập.

---

# Đợt 6 — Action urgency và execution economics thực nghiệm

## Câu hỏi

Thesis đúng nhưng nên hành động ngay, chờ thêm thông tin, thử maker hay bỏ vì edge đã chết?

## Công việc

- [ ] Action Policy sở hữu toàn bộ `ACT / WAIT / MAKER / TAKER / ABANDON`.
- [ ] Execution không suy lại LONG/SHORT; chỉ contradiction-only revalidation.
- [ ] Mỗi opportunity chạy twins/counterfactual hợp lệ:
  - `TAKER_NOW`
  - `MAKER_IF_EXECUTABLE`
  - `WAIT100`
  - `WAIT300`
  - `WAIT600`
- [ ] Maker chỉ fill khi order đã tồn tại trước trade và có touch/trade-through hợp lệ.
- [ ] Taker dùng contemporaneous executable BBO + frozen account costs.
- [ ] Không trừ spread/slippage hai lần.
- [ ] Học opportunity lifetime, time-to-support và time-to-failure theo mechanism bằng tất cả outcomes/censoring.
- [ ] Chờ evidence chỉ khi evidence có khả năng đổi action và còn đến trước khi opportunity chết.
- [ ] Thiếu sample -> `EXECUTION_URGENCY_UNVERIFIED`, demo vẫn thu mẫu; Mainnet không promote.

## PASS

- [ ] Replay deterministic, no lookahead, same-wave and fill valid.
- [ ] Kết quả dùng Guardian net after frozen cost; MFE chỉ diagnostics.
- [ ] Không có fixed multiplier/forecast giả khi cohort thiếu mẫu.

---

# Đợt 7 — Durability và failure-domain độc lập

## 7A. Off-host WAL — ưu tiên trước

- [ ] Local WAL vẫn là hot path.
- [ ] Seal immutable segments, checksum, async upload off-host.
- [ ] Upload fail không block trading; có backlog/age/storage alarms.
- [ ] Restore test dựng lại replay hash từ off-host copy.
- [ ] Không đưa S3/network vào decision loop.

## 7B. Warm standby + fencing — cần phê duyệt chi phí riêng

- [ ] Thiết kế lease/fencing token; standby `execution_authority=false` mặc định.
- [ ] Không active-active.
- [ ] Takeover phải reconcile exchange state, orders/stops, rebuild epochs và warm state trước Entry.
- [ ] Split-brain/lease partition/clock failure tests.
- [ ] Không triển khai AWS resource trả phí nếu chưa được user phê duyệt.

## 7C. Secondary execution transport — research trước

- [ ] Xác minh Binance account/API hỗ trợ transport nào cho order/control.
- [ ] Benchmark authenticated latency và failure correlation.
- [ ] Chứng minh idempotency/order identity/reconciliation giữa transports.
- [ ] Không gọi hai transport cùng exchange là hai failure domains độc lập.
- [ ] Không promote nếu fallback phụ thuộc cùng control-plane đang lỗi.

## PASS

- [ ] Host mất không làm mất audit history đã seal.
- [ ] Không bao giờ có hai execution authorities.
- [ ] Failover không mở Entry trước exchange reconciliation + protection verification.

---

# Đợt 8 — Cutover: replace, không ensemble

## Điều kiện bắt buộc trước cutover

- [ ] Canonical event runtime đã deterministic.
- [ ] Shared thesis đã được Entry/Guardian consume đúng.
- [ ] Safety/execution không rewrite truth.
- [ ] New truth/action path chạy shadow đủ version-bounded evidence.
- [ ] Baseline/new dùng cùng WAL, frozen costs, fills và current Hard Risk.
- [ ] Economic miss không tăng; false-entry không tăng material.
- [ ] PF/expectancy/LCB/stress-cost đạt gate Mainnet đã phê duyệt.
- [ ] Hard-stop rate và Guardian capture ratio không xấu hơn acceptance.
- [ ] CPU 15m/1h dưới 30%; latency/data-integrity SLO đều PASS.
- [ ] Authenticated fill/protection/reconciliation path đã được xác minh.
- [ ] Duyệt tay; không auto-promote.

## Cutover

- [ ] Một canonical Truth owner nhận authority.
- [ ] Retire imports/calls/config của legacy truth owners.
- [ ] Legacy journal reader giữ read-only compatibility.
- [ ] Không `old brain 40% + new brain 60%`.
- [ ] Có rollback manifest quay lại commit/config/schema trước cutover.

## PASS cuối

- [ ] Active launcher chỉ có một đường Truth -> Action -> Execution.
- [ ] Guardian chỉ monitor/falsify shared thesis.
- [ ] Hard Risk độc lập và fail-closed.
- [ ] Runtime soak + chaos + deterministic replay đều PASS.
- [ ] Mainnet vẫn khóa cho đến phê duyệt riêng.

## 5. Ma trận bằng chứng bắt buộc cho từng issue

| Trường | Nội dung bắt buộc |
|---|---|
| `question` | Bot cần trả lời câu gì? |
| `owner_before` | Module active nào đang trả lời? |
| `misunderstanding` | Bot đang hiểu sai cơ chế gì? |
| `evidence` | Code path, WAL, runtime trace, Binance semantics |
| `falsifier` | Bằng chứng nào bác root cause? |
| `winner_patch` | Một thay đổi coherent mạnh nhất |
| `authority_change` | Có/không; từ owner nào sang owner nào |
| `false_negative_risk` | Có làm miss thêm không? |
| `false_positive_risk` | Có mở cửa trade rác không? |
| `replay_contract` | WAL/cost/fill/Guardian/schema |
| `runtime_acceptance` | Log/state/latency/CPU cần thấy gì? |
| `rollback` | Commit/config/schema phục hồi |

## 6. Checklist cấm sửa lố

- [ ] Không thêm indicator/score/model nếu chưa có câu hỏi mới.
- [ ] Không tạo module mới khi owner hiện hữu sửa được.
- [ ] Không tăng TTL/hạ threshold để cứu vài miss cụ thể.
- [ ] Không dùng win để chứng minh reasoning đúng hoặc loss để chứng minh sai.
- [ ] Không gọi cross-venue correlation là independence.
- [ ] Không gọi OI delta là danh tính phe mở/đóng nếu thiếu evidence.
- [ ] Không gọi depth wall là absorption nếu thiếu executed response.
- [ ] Không dùng clock precision nhỏ hơn measurement uncertainty.
- [ ] Không để PnL path rewrite market truth.
- [ ] Không để process health giả thành data/strategy readiness.
- [ ] Không push khi runtime khác expected behavior.

## 7. Thứ tự ưu tiên cuối cùng

```text
P0. Evidence/determinism
P0. Fill -> verified protection + execution health
P0. Temporal/data truth
P0. Authority/semantic separation
P0. Shared thesis Entry -> Guardian
P1. Remove pseudo-authority by ablation
P1. Empirical action/execution policy
P1. Off-host WAL
P2. Warm standby/fencing and secondary transport
P2. Replace legacy brain after evidence
```

Không được nhảy thẳng tới rebuild brain nếu survival, temporal truth và replay determinism chưa PASS.

## 8. Master execution board

Đây là bảng điều phối chính. Cột `Authority impact` nói rõ patch có thể làm bot đổi hành vi hay chỉ tăng khả năng quan sát.

| ID | Ưu tiên | Work package | Câu hỏi được giải quyết | Authority impact | Phụ thuộc | Artifact bắt buộc | Gate để đi tiếp |
|---|---:|---|---|---|---|---|---|
| `G0.1` | P0 | Runtime/authority fingerprint | Code nào thật sự đang chạy? | Không | Không | `authority_map.md` | Launcher-to-order path khép kín |
| `G0.2` | P0 | Deterministic baseline | Có thể so trước/sau công bằng không? | Không | G0.1 | `baseline_manifest.json`, replay hash | Hai replay cùng hash |
| `S1.1` | P0 | Execution transaction telemetry | Fill đã được bảo vệ ở state nào? | Không | G0.2 | transaction trace | Không có transition mơ hồ |
| `S1.2` | P0 | Protection state authority | Khi nào được gọi `PROTECTED`? | Safety/Execution | S1.1 | chaos report | Chỉ exchange verification cấp state |
| `S1.3` | P0 | Idempotent emergency protocol | Exposure không bảo vệ được xử lý thế nào? | Safety | S1.2 | fault matrix | Không duplicate stop/flatten |
| `S2.1` | P0 | Control-plane latency telemetry | Exchange đang khỏe hay chỉ chưa timeout? | Không | G0.2 | latency distribution | Có p50/p95/p99 và unknown duration |
| `S2.2` | P0 | Execution health state | Có nên gửi Entry mới không? | Safety/Execution | S2.1 | state transition report | Degraded path không giết exit/reconcile |
| `T1.1` | P0 | Canonical `MarketEvent` | Event nào thực sự available lúc quyết định? | Data semantics | G0.2 | schema + golden fixtures | Live/replay cùng contract |
| `T1.2` | P0 | Epoch/gap/sequence contract | Có nối nhầm dữ liệu qua gap không? | Data semantics | T1.1 | reconnect replay | Gap luôn cắt causal continuity |
| `T1.3` | P0 | Clock uncertainty measurement | Có thật sự biết ai lead không? | Truth | T1.1 | uncertainty report | Ordering vượt measured bound |
| `T1.4` | P0 | OI/depth semantic correction | Data đang nói gì và chưa nói được gì? | Truth labels | T1.1–T1.3 | semantic regression | Không overclaim build/absorption |
| `A1.1` | P0 | Four-authority contracts | Ai sở hữu truth/action/execution/safety? | Interfaces | G0.1, T1 | contract tests | Một question = một owner |
| `A1.2` | P0 | UNKNOWN/FALSIFIED separation | Mất quan sát hay thesis sai? | Journal + Safety | A1.1 | taxonomy matrix | Không còn semantic collision |
| `A2.1` | P0 | Immutable thesis handoff | Entry bàn giao điều gì cho Guardian? | Truth lifecycle | A1 | replay diff | Thesis identity bất biến |
| `A2.2` | P0 | Shared-thesis Guardian shadow | Guardian có dựng brain thứ hai không? | Shadow only | A2.1 | paired traces | Same events, deterministic trace |
| `A2.3` | P0 | Shared-thesis Guardian cutover | Khi nào old council được retire? | Guardian live/demo | A2.2 | approval manifest | Capture/risk gates PASS |
| `Q1.x` | P1 | Pseudo-authority ablations | Rule nào có giá trị thật? | Từng rule | A1, G0.2 | one-variable reports | Mỗi removal độc lập rollback |
| `E1.1` | P1 | Action-policy ownership | Act/Wait/Maker/Taker thuộc ai? | Action | A1, A2 | action contract | Execution không suy lại market |
| `E1.2` | P1 | Execution twins | Cách khớp nào có net EV tốt hơn? | Shadow only | E1.1, T1 | twin replay | Fill/cost/Guardian canonical |
| `E1.3` | P1 | Empirical opportunity lifetime | Chờ bao lâu còn hợp lý? | Shadow rồi Action | E1.2 | all-outcome distribution | Không dùng winner-only sample |
| `D1.1` | P1 | Immutable off-host WAL | Host chết còn audit được không? | Không | T1 schema stable | restore drill | Restore cùng checksum/hash |
| `H1.1` | P2 | Standby/fencing design | Làm sao tránh host/AZ SPOF? | Chưa có | D1.1 + approval | threat model | Không active-active |
| `H1.2` | P2 | Failover rehearsal | Takeover có an toàn không? | Safety/Execution | H1.1 | failover report | Reconcile trước Entry |
| `C1.1` | P2 | Canonical brain comparison | Brain mới có thực sự tốt hơn? | Shadow only | A2, Q1, E1 | baseline/new report | Không ensemble |
| `C1.2` | P2 | Legacy authority retirement | Còn đường truth thứ hai không? | Major | C1.1 + manual approval | cutover/rollback manifest | Active graph chỉ còn một truth owner |

### Trạng thái chuẩn của mỗi work package

```text
NOT_STARTED
-> INVESTIGATING
-> ROOT_CAUSE_PROVED
-> PATCHED_LOCAL
-> STATIC_TESTED
-> REPLAY_VERIFIED
-> RUNTIME_VERIFIED
-> READY_TO_PUSH
-> COMPLETE

Nhánh lỗi:
BLOCKED_BY_DATA | HYPOTHESIS_FALSIFIED | ROLLED_BACK
```

Không được nhảy từ `INVESTIGATING` sang `PATCHED_LOCAL` nếu root cause chưa có evidence/falsifier.

## 9. Atomic delivery map

“Atomic” nghĩa là một causal concern có thể rollback độc lập, không bắt buộc một file/commit.

| Commit dự kiến | Nội dung duy nhất | Không được kèm theo |
|---|---|---|
| `C00` | Authority fingerprint + baseline manifest tooling | Strategy mutation |
| `C01` | Execution transaction telemetry | Timeout/exit behavior change |
| `C02` | Protection state transition authority | Guardian logic |
| `C03` | Emergency protection idempotency | Entry tuning |
| `C04` | Control-plane latency measurement | Tự đặt SLO khi chưa có mẫu |
| `C05` | Execution health state behavior | Market-truth labels |
| `C06` | Canonical event/time schema | Threshold changes |
| `C07` | Epoch/gap/reconnect rules | Bias/Entry change |
| `C08` | Clock uncertainty and lead semantics | Nới follower window |
| `C09` | OI/depth label correction | Direction authority mới |
| `C10` | Four-authority immutable contracts | Cutover behavior |
| `C11` | Taxonomy/journal migration | Guardian timing |
| `C12` | Frozen thesis handoff | Old Guardian removal |
| `C13` | Shared-thesis Guardian shadow | Live/demo authority switch |
| `C14` | Guardian cutover after evidence | Economics tuning |
| `C15+` | Mỗi pseudo-rule là một ablation/removal | Batch removal nhiều rule |
| `C2x` | Action-policy ownership | New truth model |
| `C3x` | Execution twins/lifetime research | Auto-promotion |
| `C4x` | Off-host WAL | S3 trong hot path |
| `C5x` | Fencing/failover | Active-active |
| `C6x` | Final cutover/legacy retire | Weighted ensemble |

## 10. Canonical state contracts

### 10.1 Market thesis lifecycle

```text
OBSERVING
-> HYPOTHESIS_OPEN
-> SUPPORTED
-> DIVERGING
-> FALSIFIED

Nguồn/state không đủ:
ANY -> UNKNOWN

Evidence phục hồi:
UNKNOWN -> HYPOTHESIS_OPEN | SUPPORTED
```

Quy tắc:

- `UNKNOWN` không phải bước trung gian bắt buộc trước `FALSIFIED`.
- Safety failure không được tự chuyển thesis sang `FALSIFIED`.
- `FALSIFIED` cần falsifier có market meaning, source/epoch hợp lệ và trace được.
- Competing explanation phải ở `SUPPORTED / CONTRADICTED / UNKNOWN`; không dùng pseudo-probability.

### 10.2 Action lifecycle

```text
NO_ACTION
-> WAIT_INFORMATION
-> ACT_TAKER_NOW | POST_MAKER | ABANDON
```

Mỗi action phải ghi:

- thesis version và episode ID;
- evidence còn thiếu có thể đổi quyết định;
- opportunity lifetime status;
- frozen execution/cost assumptions;
- expiry và falsifiers;
- `FALLBACK_ESTIMATE` nếu phải dùng fallback thay causal certainty.

### 10.3 Execution transaction lifecycle

```text
INTENT_CREATED
-> SUBMITTING
-> ACKNOWLEDGED | REJECTED | EXECUTION_UNKNOWN
-> PARTIALLY_FILLED | FILLED | CANCELLED

FILLED
-> UNPROTECTED_EXPOSURE
-> PROTECTION_SUBMITTING
-> PROTECTION_ACKNOWLEDGED
-> PROTECTION_VERIFIED
-> PROTECTED_POSITION
```

Forbidden transitions:

- `FILLED -> PROTECTED_POSITION` không qua exchange verification.
- `EXECUTION_UNKNOWN -> RESUBMIT` không reconcile order identity.
- `PROCESS_RESTART -> ENTRY_READY` không reconcile account/data/epochs.
- `ACTION_EXPIRED -> EXECUTE` dù exchange path vừa hồi phục.

### 10.4 Runtime readiness lifecycle

```text
PROCESS_UP
-> SOURCES_CONNECTED
-> SEQUENCES_RECONCILED
-> CLOCK_QUALITY_KNOWN
-> ACCOUNT_RECONCILED
-> STRATEGY_WARM
-> ENTRY_READY
```

Demo có thể tiếp tục recorder/research ở các state thấp hơn; không được giả `ENTRY_READY`.

## 11. Minimal schemas cần khóa trước khi code

### `MarketEventV1`

| Field | Ý nghĩa | Quyền sử dụng |
|---|---|---|
| `event_id` | Identity/dedupe | Data authority |
| `source`, `stream` | Provenance | Tất cả consumers |
| `exchange_event_time` | Thời gian sàn tuyên bố | Ordering evidence |
| `receive_time_monotonic` | Host nhận byte/event | Latency |
| `available_time_monotonic` | Event sẵn sàng cho decision | Live/replay truth |
| `sequence`, `epoch` | Continuity | Gap/reconnect guard |
| `health` | Fresh/degraded/dead/contradictory | Evidence authority |
| `payload_version` | Schema binding | Replay compatibility |

### `MarketThesisVNext`

| Field | Ý nghĩa |
|---|---|
| `thesis_id`, `version`, `causal_episode_id` | Immutable identity |
| `mechanism` | Mô tả cơ chế, không phải indicator label |
| `side` | Direction nếu evidence cho phép, nếu không UNKNOWN |
| `status` | Supported/diverging/falsified/unknown |
| `supporting_evidence_refs` | Event references, không copy truth mơ hồ |
| `competing_explanations` | Tối thiểu các hypothesis còn sống |
| `falsifiers` | Evidence có thể giết thesis |
| `expected_next` | Observation tiếp theo có meaning |
| `source_requirements` | Nguồn nào bắt buộc/có thể degrade |
| `measurement_quality` | Clock/gap/epoch/data validity |

### `ActionIntentVNext`

| Field | Ý nghĩa |
|---|---|
| `action_id` | Idempotent intent identity |
| `thesis_id/version` | Truth được consume |
| `action` | Taker/maker/wait/abandon |
| `opportunity_expiry` | Không submit intent đã chết |
| `frozen_cost_plan` | Cost contract một lần |
| `execution_constraints` | BBO/spread/qty/filter/episode |
| `hard_contradictions` | Chỉ lý do Execution được cancel |

### `SafetyDecisionVNext`

| Field | Ý nghĩa |
|---|---|
| `system_health` | Operational truth |
| `risk_action` | Block/reduce/exit/halt |
| `reason` | Hard risk, observability, reconciliation... |
| `thesis_status_unchanged` | Invariant audit flag |

## 12. Verification pyramid

| Tầng | Mục tiêu | Dữ liệu | PASS |
|---|---|---|---|
| Static contract | Cấm owner viết field của owner khác | Type/schema/AST tests | Không mutation chéo authority |
| Unit state machine | Transition hợp lệ | Synthetic fixtures | Mọi forbidden transition bị reject |
| Deterministic replay | No lookahead và semantic ổn định | Cùng versioned WAL | Hai run cùng hash |
| Differential replay | Patch hiểu tốt hơn baseline? | Same WAL/cost/Guardian | Root-case sửa, controls không xấu |
| Counterfactual execution | Fill/cost/action thật hơn | BBO/trades/latency | Chỉ executable outcome được học |
| Chaos | Process/network/exchange failure | Fault injection | Không unprotected silent state |
| Shadow runtime | Event loop và live sources | Live public/auth-safe | Expected journal/state xuất hiện |
| Authenticated safe test | ACK/stop/private stream/reconcile | Testnet hoặc bounded safe path | Không nhãn giả VERIFIED |
| Soak | Reliability/CPU/I/O | 72h version-bounded | SLO/CPU/data continuity PASS |

### Regression corpus bắt buộc

- [ ] Good trend continuation.
- [ ] Cash-led reversal/control transfer.
- [ ] Futures-led sweep không cash acceptance.
- [ ] Forced unwind/liquidation tail.
- [ ] Flow mạnh nhưng price không convert.
- [ ] Dual-cash correlated move nhưng causal leader unresolved.
- [ ] Coinbase degraded/stale/dead.
- [ ] OI unchanged/stale/fresh delta.
- [ ] Depth wall cancel không executed flow.
- [ ] Reconnect/gap/out-of-order/clock drift.
- [ ] Decision đúng nhưng submit stale.
- [ ] Partial fill rồi protection timeout.
- [ ] Process restart với unknown exchange state.
- [ ] Guardian premature exit, late exit và correct loss.
- [ ] Safety exit trong khi thesis vẫn supported/unknown.

## 13. Acceptance metrics không được trộn nghĩa

### Market-understanding quality

- Mechanism classification phải có evidence refs và falsifier.
- `UNKNOWN` rate được báo cáo riêng; không ép giảm bằng overclaim.
- Control-transfer detection đo theo causal replay, không nhìn chart hậu nghiệm.
- Winning trade với sai mechanism vẫn tính reasoning failure.

### Opportunity quality

- `ECONOMIC_MISS_CONFIRMED` chỉ khi same wave + feed valid + fill feasible + frozen cost + Guardian counterfactual net dương.
- False entry: causal thesis bị falsify sớm hoặc net edge âm do setup, tách khỏi execution failure.
- Premature exit: current Guardian exit trước khi shared thesis falsify và counterfactual net/capture tốt hơn.
- Không dùng raw MFE làm lợi nhuận.

### Execution/safety quality

- Protection coverage và fill-to-protection distributions.
- Order ambiguity duration và reconciliation success.
- Decision-to-submit/ACK latency so với opportunity expiry.
- Duplicate/missing order/trade/stop count phải bằng 0.

### Infrastructure quality

- CPU toàn host: mọi rolling 15m/1h `<30%`.
- Event-loop lag, WS receive-to-available, WAL fsync, disk queue và control-plane latency có SLO riêng.
- Recorder degradation không được làm Guardian/Hard Risk mù.
- CPU PASS không được dùng thay cho latency/data-integrity PASS.

## 14. Promotion and rollback manifest

Mỗi work package phải xuất manifest tương đương:

```yaml
change_id: A2.2
baseline_commit: <sha>
candidate_commit: <sha>
runtime_mode: SHADOW
config_hash: <hash>
event_schema: <version>
journal_schema: <version>
wal_segments: [<immutable ids>]
frozen_cost_version: <version>
guardian_version: <version>
authority_before: <owner>
authority_after: <owner-or-unchanged>
replay_hashes: [<run1>, <run2>]
tests:
  static: PASS|FAIL
  replay: PASS|FAIL
  chaos: PASS|FAIL|NOT_APPLICABLE
  runtime: PASS|FAIL
cpu_15m_max_pct: <value>
cpu_1h_max_pct: <value>
authenticated_path: VERIFIED|UNVERIFIED
known_unknowns: []
promotion_decision: HOLD|APPROVE_SHADOW|APPROVE_DEMO|MANUAL_LIVE_REVIEW
rollback_commit: <sha>
```

Rollback bắt buộc nếu:

- deterministic hash drift không giải thích được;
- semantic owner bị trùng;
- impossible transition xuất hiện;
- economic miss tăng rõ hoặc false entry tăng material;
- Guardian capture/risk regression;
- duplicate execution/protection;
- rolling CPU vượt 30%;
- source health/data epoch bị nối sai;
- runtime khác contract dù unit tests PASS.

## 15. Risk register

| Risk | Severity | Dấu hiệu sớm | Mitigation | Owner |
|---|---:|---|---|---|
| Fill chưa protected | Critical | Fill không có verified stop trace | Execution transaction + emergency protocol | Hard Risk/Execution |
| Hai truth owners | Critical | Cùng event ra hai thesis đối nghịch | Question/owner invariant | Architecture |
| UNKNOWN bị gọi false | High | Safety exit làm thesis label broken | Taxonomy separation | Truth/Safety |
| Replay lookahead | Critical | Historical result đẹp hơn live bất thường | `available_time` authority | Data/Replay |
| False causal lead | High | Lead gap <= uncertainty | Measured clock contract | Data/Truth |
| OI semantic overclaim | High | Một delta tự tạo direction | Multi-hypothesis context only | Truth |
| Depth spoof authority | High | Wall/cancel tạo action không trades | Executed-response requirement | Truth |
| Guardian brain duplication | High | Guardian council trái frozen thesis | Shared-thesis monitor | Guardian |
| Stale action submitted | Critical | ACK sau opportunity expiry | Pre-submit expiry/health | Action/Execution |
| Duplicate order after timeout | Critical | Unknown request rồi resubmit | Idempotency + reconcile | Execution |
| Recorder steals hot-path I/O | Medium | WAL fsync/event-loop lag spike | Async/bounded recorder | SRE |
| Host loss destroys evidence | High | Local-only sealed WAL | Async off-host durability | SRE |
| Split brain | Critical | Hai nodes có execution authority | Fencing lease/token | SRE/Execution |
| Score soup returns | High | New/old brain weighted together | Replace-only cutover | Architecture |
| Overfit to known misses | High | Patch passes only named timestamps | Out-of-sample control corpus | Replay |

## 16. Per-issue implementation ticket

Sao chép khối này cho từng mutation:

```markdown
### ISSUE <ID> — <name>

Question owner:
Observed mechanism:
Competing explanation:
Evidence in active path:
Falsifier:
What the bot misunderstands:
Why it causes miss/loss/safety risk:
Winner patch:
Files/symbols on active path:
Authority change: NONE | FROM -> TO
Schema/version impact:
False-negative risk:
False-positive risk:

Tests:
- [ ] static/unit
- [ ] deterministic replay x2
- [ ] differential controls
- [ ] chaos if relevant
- [ ] runtime trace
- [ ] CPU/I/O

PASS:
FAIL/ROLLBACK:
Known unknowns:
```

## 17. Stop/go checklist giữa các đợt

### G0 -> Đợt 1

- [ ] Active authority graph khép kín.
- [ ] Baseline deterministic.
- [ ] Mainnet khóa.

### Đợt 1 -> Đợt 2

- [ ] Fill/protection states không mơ hồ.
- [ ] Unknown execution reconcile được.
- [ ] Control-plane measurement không gây hot-path regression.

### Đợt 2 -> Đợt 3

- [x] Canonical available-time live/replay.
- [x] Gap/epoch/clock tests PASS.
- [x] OI/depth không overclaim.

### Đợt 3 -> Đợt 4

- [x] Four-authority contracts ổn định.
- [x] UNKNOWN/FALSIFIED/SYSTEM_UNSAFE tách rõ.
- [x] Contract-only diff không đổi decisions.

### Đợt 4 -> Đợt 5

- [ ] Shared thesis trace deterministic.
- [ ] Guardian shadow không giảm capture/risk quality.
- [ ] Không còn duplicate truth authority trên candidate path.

### Đợt 5 -> Đợt 6

- [ ] Mỗi pseudo-rule có ablation riêng.
- [ ] Không xóa rule vì “trông xấu”; phải có causal/empirical result.

### Đợt 6 -> Đợt 7

- [ ] Action/Execution owner không conflict.
- [ ] Fill twins canonical; costs không double-count.
- [ ] Lifetime model dùng all outcomes.

### Đợt 7 -> Đợt 8

- [ ] Off-host restore drill PASS.
- [ ] HA/fallback nếu triển khai có phê duyệt và fencing proof.
- [ ] Không phát sinh execution authority thứ hai.

### Cutover

- [ ] Manual approval.
- [ ] Rollback rehearsal.
- [ ] Mainnet vẫn khóa cho tới một quyết định riêng sau authenticated verification.

## 18. Definition of Done

Kế hoạch chỉ được coi hoàn thành khi:

- [ ] Một canonical event/time model phục vụ live, recorder và replay.
- [ ] Một market-truth owner duy nhất trên active launcher path.
- [ ] Action, Execution và Safety không suy lại market theo logic riêng.
- [ ] Guardian monitor/falsify cùng frozen thesis.
- [ ] UNKNOWN, falsification và system failure không còn bị trộn.
- [ ] Fill-to-protection transaction có exchange-verified terminal state.
- [ ] Execution-control health chặn Entry trước khi control plane unsafe.
- [ ] Magic/pseudo numbers đã bị retire hoặc hạ thành prior bằng ablation.
- [ ] Empirical economics chỉ học executable, same-wave, no-lookahead outcomes.
- [ ] Off-host WAL phục hồi được; HA nếu có không thể split-brain.
- [ ] CPU rolling 15m/1h `<30%` và latency/data-integrity SLO cùng PASS.
- [ ] Legacy truth authority bị retire, không ensemble.
- [ ] Replay, chaos, shadow soak và authenticated safety path đều có artifact.
- [ ] Mainnet chỉ được xem xét bằng phê duyệt riêng; không auto-promote.

## 19. Definition of Not Done

Các tình trạng sau không được báo “đã xong”:

- Unit tests PASS nhưng chưa runtime verify.
- Recorder có dữ liệu nhưng replay không deterministic.
- Process active nhưng source/account/strategy chưa ready.
- Stop order local state tồn tại nhưng exchange chưa verify.
- Có `confidence=high` nhưng không có calibrated/causal evidence.
- Một vài miss cũ được cứu nhưng out-of-sample false entry tăng.
- New brain chạy song song và vote với old brain.
- CPU thấp nhưng event-loop/control-plane latency vi phạm.
- Public Binance market data hoạt động nhưng authenticated execution path chưa test.
- GitHub có commit nhưng deployed runtime không đúng commit/config.

---

## Quy tắc bắt đầu thực thi sau tài liệu này

Lệnh tiếp theo chỉ được bắt đầu ở **Đợt 0**. Không code thẳng Đợt 4/5 từ hai báo cáo. Sau mỗi work package phải cập nhật Master execution board bằng artifact và trạng thái thật; không đánh dấu PASS theo cảm giác.
