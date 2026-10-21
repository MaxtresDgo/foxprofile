<div align="center">
  <img src="src/assets/icon.png" width="112" alt="FoxProfile logo" />
  <h1>FoxProfile</h1>
  <p><strong>Quản lý nhiều profile trình duyệt chống phát hiện, miễn phí và mã nguồn mở</strong></p>
  <p>Mỗi profile có fingerprint thiết bị, cookie và proxy riêng · Hai engine: Camoufox (Firefox) và Chrome · Giao diện tiếng Anh và tiếng Việt · REST API và MCP cho tự động hóa</p>

  <p>
    <a href="https://github.com/chienbm98/foxprofile/actions/workflows/ci.yml"><img src="https://github.com/chienbm98/foxprofile/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
    <a href="https://github.com/chienbm98/foxprofile/releases/latest"><img src="https://img.shields.io/github/v/release/chienbm98/foxprofile" alt="Release" /></a>
    <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" />
    <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Windows | macOS | Linux" />
    <a href="LICENSE"><img src="https://img.shields.io/github/license/chienbm98/foxprofile" alt="MIT License" /></a>
    <a href="https://buymeacoffee.com/chienbm98"><img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-%E2%98%95-FFDD00?logo=buymeacoffee&logoColor=black" alt="Buy me a coffee" /></a>
  </p>

  <p><a href="README.md">English</a> · Tiếng Việt</p>
  <p>
    <a href="#bắt-đầu-nhanh">Bắt đầu nhanh</a> ·
    <a href="#tính-năng">Tính năng</a> ·
    <a href="#rest-api">REST API</a> ·
    <a href="#mcp-cho-ai-điều-khiển-trình-duyệt">MCP</a> ·
    <a href="#đóng-góp">Đóng góp</a>
  </p>
</div>

![FoxProfile](docs/images/main.png)

FoxProfile quản lý các profile trình duyệt chống phát hiện: mỗi profile là một danh tính riêng với fingerprint thiết bị, cookie, proxy, timezone và locale của nó. Trình duyệt mặc định là [Camoufox](https://github.com/daijro/camoufox), một bản Firefox được chỉnh sửa ở tầng mã nguồn để giả lập fingerprint; ngoài ra có engine Chrome thử nghiệm dựa trên [fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium). FoxProfile là lựa chọn tự host, chạy trên máy bạn hoặc VPS, thay cho các antidetect browser trả phí như GoLogin, GPM hay MoreLogin.

## Mục lục

- [Tính năng](#tính-năng)
- [Yêu cầu](#yêu-cầu)
- [Bắt đầu nhanh](#bắt-đầu-nhanh)
- [Timezone, locale và Kiểm tra IP](#timezone-locale-và-kiểm-tra-ip)
- [Chế độ server và web panel](#chế-độ-server-và-web-panel)
- [MCP: cho AI điều khiển trình duyệt](#mcp-cho-ai-điều-khiển-trình-duyệt)
- [Chạy bước và đồng bộ](#chạy-bước-và-đồng-bộ)
- [Engine Chrome (thử nghiệm)](#engine-chrome-thử-nghiệm)
- [Chuyển từ AdsPower](#chuyển-từ-adspower)
- [Cookie và fingerprint](#cookie-và-fingerprint)
- [Cấu hình](#cấu-hình)
- [REST API](#rest-api)
- [Phát triển](#phát-triển)
- [Đóng góp](#đóng-góp)
- [Ủng hộ](#ủng-hộ)
- [Lưu ý sử dụng](#lưu-ý-sử-dụng)
- [Giấy phép](#giấy-phép)
- [Ghi công](#ghi-công)

## Tính năng

- **Fingerprint cố định cho từng profile.** Lần mở đầu tiên sinh ra một thiết bị (màn hình, GPU, số nhân CPU, font, nhiễu canvas/audio) và lưu lại; các lần sau mở lại đúng thiết bị đó. Có thể chủ động tạo fingerprint mới.
- **Hai engine.** Camoufox (Firefox, mặc định) hoặc Chrome (fingerprint-chromium, thử nghiệm), chọn khi tạo profile. Bản Chrome được tự tải về khi cần.
- **Giữ phiên làm việc.** Lịch sử, cookie và đăng nhập nằm trong thư mục của profile; các tab đang mở được mở lại ở lần khởi động sau.
- **Mỗi profile một proxy riêng**, hỗ trợ HTTP/HTTPS/SOCKS4/SOCKS5 có hoặc không có mật khẩu, kể cả dạng `host:port:user:pass`, có nút kiểm tra proxy.
- **Timezone và locale** tự khớp theo IP hoặc đặt cố định cho từng profile. Nút **Kiểm tra IP** đối chiếu IP ra với Cloudflare, ipinfo, ip-api và cảnh báo khi lệch quốc gia/timezone hoặc IP bị gắn datacenter.
- **Xuất/nhập cookie** dạng JSON Cookie-Editor / EditThisCookie (GoLogin, GPM, Multilogin, extension Cookie-Editor) và `cookies.txt` Netscape (yt-dlp, curl, wget).
- **Xuất/nhập cả profile** ra file ZIP để chuyển sang máy khác, có hoặc không kèm dữ liệu trình duyệt. Fingerprint luôn đi kèm.
- **Bảng profile dễ quét**: mỗi dòng cho thấy engine, thiết bị đang giả lập, proxy (đã ẩn mật khẩu), timezone/locale và trạng thái. Trạng thái phân biệt bằng cả hình lẫn màu: dấu tím viền kép kèm giờ mở là đang chạy, viền xám là đang mở, dấu đỏ gạch ngang là mở lỗi.
- **Lọc và tìm kiếm**: lọc theo đang chạy, có/không proxy, engine Chrome (kèm số đếm); tìm theo tên hoặc proxy.
- **Tài khoản mạng xã hội**: lưu tài khoản Facebook, Instagram, TikTok, X, Gmail và Hotmail/Outlook của từng profile (tên đăng nhập, mật khẩu, mã bí mật 2FA), được mã hoá khi lưu; sao chép, xem mã 2FA hiện tại hoặc tự điền form đăng nhập trong profile đang chạy. Rê chuột lên tên profile để xem các tài khoản của nó, và mũi tên trên nút Mở cho phép mở profile rồi đăng nhập bằng một tài khoản. Cũng được nhập từ AdsPower. Không mở qua MCP.
- **Thao tác hàng loạt**: chọn nhiều profile để mở, dừng, sửa hoặc xóa cùng lúc. Sửa hàng loạt chỉ đổi các trường được đánh dấu (hệ điều hành, proxy, timezone, locale; ở chế độ hub thêm người dùng và nhóm), các trường khác của từng profile giữ nguyên.
- **Chạy bước và đồng bộ** (RPA đơn giản): chạy một đoạn script ngắn (navigate, click, type…) song song trên nhiều profile đang chạy, hoặc lặp lại thao tác của một profile sang các profile khác theo thời gian thực.
- **MCP server**: Claude, Cursor... tự mở profile, lướt web, click, gõ, chụp màn hình.
- **Chế độ server + web panel**: chạy trên VPS, quản lý và xem màn hình từ xa qua trình duyệt, có token bảo vệ.
- **REST API** để điều khiển bằng script, có trang tài liệu Swagger.
- Giao diện **tiếng Anh** (mặc định) và tiếng Việt, đổi ngay trong app. Font đóng gói sẵn, không cần mạng.

## Yêu cầu

- Python 3.10 trở lên
- Windows, macOS hoặc Linux (CI chạy test trên cả ba)
- Vài trăm MB dung lượng trống cho Camoufox và cơ sở dữ liệu GeoIP, thêm ~140-190 MB nếu dùng engine Chrome

## Bắt đầu nhanh

```bash
git clone https://github.com/chienbm98/foxprofile.git
cd foxprofile

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/macOS

pip install -r requirements.txt
python -m camoufox fetch
```

`camoufox fetch` tải bản trình duyệt Camoufox (vài trăm MB) từ GitHub của dự án Camoufox. Lần mở profile đầu tiên sẽ tải thêm cơ sở dữ liệu GeoIP (~50 MB).

Chạy ứng dụng:

```bash
python -m src.main
```

Trên Windows có thể bấm đúp `run_foxprofile.bat`. Ứng dụng mở cửa sổ quản lý và đồng thời chạy API tại `http://127.0.0.1:8000`; tài liệu API ở `http://127.0.0.1:8000/docs`. Đặt `FOXPROFILE_LANG=vi` hoặc `FOXPROFILE_LANG=es` để dùng giao diện tiếng Việt hoặc tiếng Tây Ban Nha.

## Timezone, locale và Kiểm tra IP

Mặc định, mỗi lần mở profile Camoufox tra IP đi ra (qua proxy của profile) và đặt timezone, locale, toạ độ và IP WebRTC theo IP đó. Có ba điều nên biết:

- **Locale tự động được chọn ngẫu nhiên ở mỗi lần mở**, theo tỷ lệ người nói từng ngôn ngữ ở quốc gia của IP. Ví dụ IP Pháp cho `fr-FR` khoảng 60% số lần, còn lại là `en-FR`, `es-FR`… Một tài khoản đổi ngôn ngữ giữa các phiên là điều đáng ngờ, nên hãy đặt cố định locale cho profile.
- **Các cơ sở dữ liệu GeoIP không phải lúc nào cũng thống nhất.** Cùng một IP có thể được Cloudflare xếp vào nước này, Google vào nước khác; Camoufox cũng có thể chọn một timezone cùng giờ nhưng khác tên (ví dụ IP Việt Nam ra `Asia/Bangkok` thay vì `Asia/Ho_Chi_Minh`).
- **IP datacenter dễ bị bắt captcha** bất kể cấu hình trình duyệt. Với các trang dùng Cloudflare, proxy residential hoặc 4G quan trọng hơn mọi tinh chỉnh khác.

![Hộp thoại tạo profile](docs/images/profile-dialog.png)

Trong hộp thoại tạo/sửa profile có hai ô **Timezone** (tên IANA, ví dụ `Asia/Ho_Chi_Minh`) và **Locale** (dạng `vi-VN`); để trống là tự động theo IP. Nút **Kiểm tra IP** lấy IP đi ra qua proxy, đối chiếu với Cloudflare, ipinfo và ip-api, rồi cảnh báo khi lệch quốc gia hoặc timezone, khi IP bị gắn datacenter, khi proxy ra Internet bằng IPv4 và IPv6 khác nhau, và khi locale đang để ngẫu nhiên.

## Chế độ server và web panel

Không cần cửa sổ desktop: chỉ chạy API và web panel, trình duyệt chạy headless. Phù hợp để chạy trên VPS.

```bash
python -m src.server                     # http://127.0.0.1:8000
python -m src.server --host 0.0.0.0      # mở ra mạng: BẮT BUỘC đặt token
```

Mở `http://<địa-chỉ>:8000/` để vào **web panel**: cùng bảng profile, bộ lọc và thao tác hàng loạt như app desktop; tạo, sửa, mở/dừng profile, xuất/nhập cookie và **xem màn hình từ xa**. Bạn có thể bấm thẳng lên ảnh màn hình và gõ phím để tự đăng nhập hay giải captcha, kể cả khi trình duyệt đang chạy headless trên server.

| Web panel | Xem màn hình từ xa |
| --- | --- |
| ![Web panel](docs/images/panel.png) | ![Xem màn hình từ xa](docs/images/panel-viewer.png) |

**Bảo mật:** khi nghe ngoài `127.0.0.1`, server **từ chối khởi động** nếu chưa đặt `FOXPROFILE_API_TOKEN` (tối thiểu 24 ký tự). Web panel, REST API và MCP đều dùng chung token này.

```bash
# Tạo token ngẫu nhiên
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Khi đưa lên VPS, nên đặt server sau reverse proxy có HTTPS (Caddy, Nginx) để token không bị gửi đi dưới dạng chữ thường. Trên Linux không có màn hình, đặt `FOXPROFILE_HEADLESS=virtual` (cần cài `xvfb`) để trình duyệt chạy trên màn hình ảo, khó bị phát hiện hơn chế độ headless thường.

Hướng dẫn triển khai đầy đủ (Ubuntu, systemd, nginx, HTTPS): [docs/DEPLOY.md](docs/DEPLOY.md).

**Chế độ hub (server cho nhóm, trình duyệt chạy trên từng máy):** `docker compose up -d` chạy panel trung tâm với tài khoản riêng cho từng nhân viên, đăng nhập qua nền tảng định danh của bạn (hub không lưu mật khẩu); mỗi người chạy `foxprofile-agent.bat` / `foxprofile-agent.command` (hoặc các bộ cài, đã kèm Python và tự chạy agent khi đăng nhập: `FoxProfileAgent-<version>.msi` trên Windows, `FoxProfileAgent-<version>-macos.pkg` trên Mac Apple silicon chạy macOS 15 trở lên) và profile mở trên chính máy của họ. Agent đăng nhập qua trình duyệt (panel hỏi nhân viên cấp quyền cho agent, không cần mật khẩu trên máy) và chạy ở khay hệ thống / thanh menu, với menu để đổi tài khoản hoặc xem log. Agent cài bằng bộ cài tự cập nhật theo phiên bản của hub (chỉ phần code; thư viện mới cần bộ cài mới), và mục Cài đặt › Agent trong panel cung cấp bộ cài (từ ổ đĩa của hub hoặc bucket S3 riêng tư như RustFS, qua URL ký sẵn) và cho xem phiên bản của từng máy. Nhóm cho phép nhiều nhân viên dùng chung một số profile từ các máy khác nhau. Hướng dẫn (tiếng Tây Ban Nha): [docs/HUB.md](docs/HUB.md).

## MCP: cho AI điều khiển trình duyệt

FoxProfile có sẵn MCP server, để Claude Code, Claude Desktop, Cursor, VS Code... tự quản lý profile và thao tác trên trang: mở URL, đọc trang, click, gõ, chụp màn hình.

**Cách nhanh nhất:** bấm **Kết nối AI (MCP)** ở thanh bên trái của app desktop hoặc web panel. Hộp thoại sinh sẵn cấu hình cho từng ứng dụng AI, đúng địa chỉ và token của bạn, chỉ cần bấm Sao chép rồi dán.

![Kết nối AI (MCP)](docs/images/mcp-guide.png)

MCP được phục vụ qua HTTP tại `/mcp`, ngay trên cổng của FoxProfile, nên dùng được cả khi FoxProfile chạy trên VPS:

```bash
# Claude Code
claude mcp add --transport http foxprofile http://127.0.0.1:8000/mcp
# Server có token:
claude mcp add --transport http foxprofile https://vps.cua-ban.com/mcp --header "Authorization: Bearer <token>"
```

```jsonc
// Cursor: ~/.cursor/mcp.json
{ "mcpServers": { "foxprofile": { "url": "http://127.0.0.1:8000/mcp" } } }
```

Claude Desktop chỉ chạy được server MCP cục bộ: dùng `foxprofile_mcp.py` (cùng máy) hoặc cầu nối `npx mcp-remote` (máy khác). Hộp thoại "Kết nối AI" sinh sẵn cả hai.

Sau đó chỉ cần nhắn AI, ví dụ: *"Mở profile tiktok-us-02, vào tiktok.com và chụp màn hình cho tôi"*.

| Nhóm | Tool |
| --- | --- |
| Profile | `list_profiles`, `get_profile`, `create_profile`, `update_profile`, `launch_profile`, `stop_profile`, `running_profiles` |
| Cookie & fingerprint | `export_cookies`, `import_cookies`, `get_fingerprint`, `reset_fingerprint`, `check_proxy`, `check_profile_ip` |
| Trang | `browser_navigate`, `browser_back`, `browser_snapshot`, `browser_get_text`, `browser_click`, `browser_click_at`, `browser_type`, `browser_press`, `browser_wait_for`, `browser_screenshot`, `browser_evaluate` |
| Tab | `browser_tabs`, `browser_tab_new`, `browser_tab_select`, `browser_tab_close` |
| Nhiều profile | `run_steps` |

MCP **không** có tool xóa profile, để AI không thể vô tình xóa cookie của tài khoản. Trang chỉ mở được `http`, `https` và `about:blank`; `file://` và `about:config` bị chặn.

## Chạy bước và đồng bộ

Chọn các profile đang chạy trong bảng (app desktop hoặc web panel) rồi dùng thanh thao tác hàng loạt:

**Chạy bước** chạy một đoạn script ngắn trên mọi profile đã chọn cùng lúc, mỗi dòng một bước. Tham số hai phần ngăn cách bằng ` | `, dòng bắt đầu bằng `//` là chú thích:

```text
navigate https://example.com
click text=Sign in
type input[name="email"] | user@example.com
select select[name="country"] | VN
press Enter
wait #dashboard
sleep 2000
```

Các bước: `navigate <url>`, `back`, `click <selector>`, `type <selector> | <text>`, `select <selector> | <value>`, `press <key> [| <selector>]`, `wait <selector>`, `sleep <ms>` (tối đa 60000), `keyboard <text>`, `click_at <x> <y>`, `evaluate <js>`, `tab_new [url]`, `tab_select <i>`, `tab_close <i>`. Selector là selector Playwright, giống API điều khiển trang. Tối đa 100 bước mỗi lần chạy. Trong một profile các bước chạy theo thứ tự; profile dừng ở bước lỗi đầu tiên, các profile khác vẫn chạy tiếp. Kết quả ghi bước lỗi của từng profile.

**Đồng bộ** chọn một profile làm profile chính. Trong khi đồng bộ, click, chữ đã gõ, thay đổi `<select>` và Enter/Tab/Escape trên các trang của profile chính, cùng địa chỉ gõ vào profile chính, được lặp lại ở tab đang mở của các profile đã chọn còn lại. Dùng được cả với trình duyệt chạy ẩn: thao tác trên profile chính qua màn hình từ xa. Cần biết:

- Phần tử được tìm theo selector (id, `name`, `aria-label`…, nếu không có thì theo vị trí trong trang). Profile có trang khác đi (ngôn ngữ khác, A/B test) sẽ lỗi ở bước đó, và lỗi hiện trên nhãn đồng bộ.
- Một bước chỉ được lặp lại ở profile đang ở **cùng trang web** (origin) với profile chính, nên profile đã đi sang trang khác không bao giờ bị click nhầm vào trang đó.
- Mỗi profile chỉ ở trong một phiên đồng bộ, và không thể vừa là profile chính của phiên này vừa là profile theo của phiên khác.
- Đồng bộ cài một listener nhỏ vào các trang của profile chính, tồn tại đến khi trình duyệt đó khởi động lại (không làm gì khi không đồng bộ). Với engine Chrome, script của trang có thể thấy nó; với Camoufox nó tách biệt khỏi trang.

Qua API: `POST /api/v1/automation/run` với `{"profiles": [...], "script": "..."}` (hoặc `steps` dạng JSON), và `POST` / `GET` / `DELETE /api/v1/automation/sync`. Tool MCP `run_steps` cũng chạy được script; đồng bộ chỉ dành cho người dùng, không có trong MCP.

## Engine Chrome (thử nghiệm)

Ngoài Camoufox (Firefox), profile có thể chạy trên engine **Chrome**: một bản Chromium đã vá fingerprint ở tầng C++ ([fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium), giấy phép BSD-3). Chọn ở ô **Engine** khi tạo profile; không đổi được sau khi tạo. Bản trình duyệt (~140-190 MB tuỳ hệ điều hành, tải từ GitHub của fingerprint-chromium và kiểm tra SHA-256) được tự tải ở nền ngay khi tạo profile Chrome đầu tiên, hoặc lúc mở profile nếu chưa có; tiến độ hiện trong log. Muốn tải trước bằng tay: `python -m chrome_engine fetch`. Các bộ cài agent (MSI, pkg macOS) đã kèm sẵn.

Nên chọn đúng hệ điều hành của máy đang chạy FoxProfile: persona khác hệ điều hành sẽ lộ font của máy, và persona Linux trên Windows/macOS bị từ chối vì WebGL lộ GPU thật. Hỗ trợ Windows x64, macOS Apple Silicon và Linux x64 (chưa kiểm chứng). Chi tiết và kết quả kiểm thử: [chrome_engine/README.md](chrome_engine/README.md).

**Profile Android (điện thoại).** Với engine Chrome, hệ điều hành còn có thể là **Android**: profile hiện ra như một điện thoại (User-Agent Chrome Android, client hints và `navigator.platform` khớp theo, màn hình điện thoại, cảm ứng, con trỏ coarse) trên bất kỳ máy Windows hay Mac nào. Tính năng này dùng để chuyển profile Android từ các trình duyệt anti-detect khác mà giữ nguyên User-Agent trang web đã biết. Giới hạn đã biết: font desktop của máy lộ qua đo font, WebGL báo GPU desktop, và `navigator.platform` bên trong web worker là của máy.

## Chuyển từ AdsPower

**Nhập từ AdsPower** (web panel: Settings; app desktop: nút **Từ AdsPower** ở thanh bên) sao chép profile từ AdsPower đang chạy trên cùng máy, qua Local API của nó (bật trong AdsPower: Settings > API; nhập API key nếu đã bật). Danh sách được nhóm theo nhóm của AdsPower, có số đếm cho từng nhóm (đã nhập, còn lại, lỗi) để kiểm tra một lần chuyển lớn theo từng nhóm; tích cả nhóm hoặc từng profile rồi nhập. Nhập lại chỉ thêm những profile chưa có; tích **Nhập lại các profile đã có** để làm mới cookie của chúng từ AdsPower (gộp: cookie của AdsPower thay cookie cùng tên và domain; proxy, tài khoản, fingerprint và dữ liệu trình duyệt giữ nguyên, profile đang mở bị bỏ qua). AdsPower chỉ trả lời một yêu cầu mỗi giây, nên mỗi profile mất khoảng 8-10 giây. Khi bấm nhập, hộp thoại đóng lại và việc nhập chạy nền: một thông báo luôn hiện tiến độ ("Đang nhập 3/10 từ AdsPower: <profile>" cùng số lỗi), profile hiện dần trong danh sách, và một thông báo kết quả khi xong. Hãy để tab panel (hoặc app desktop) mở cho đến lúc đó.

| Được chuyển | Không chuyển được |
|---|---|
| Tên (sửa cho hợp lệ làm tên thư mục; tên trùng được thêm số của AdsPower) | Nhiễu fingerprint (canvas, audio, client rects, ảnh WebGL): thuật toán riêng của SunBrowser, trình duyệt khác không tái tạo được |
| Proxy, kể cả proxy chọn từ danh sách proxy của AdsPower | Local storage, extension, lịch sử |
| Đúng User-Agent, từ đó suy ra hệ điều hành; profile Android thành điện thoại Android (xem trên) | Ghi chú, nhóm |
| Cookie (Chrome giới hạn hạn dùng 400 ngày, giống SunBrowser) | Tài khoản nền tảng của trang mà FoxProfile không lưu tài khoản (báo bằng cảnh báo) |
| Tài khoản nền tảng (tên đăng nhập, mật khẩu, khoá 2FA) khi trang là Facebook, Instagram, TikTok, X, Google hoặc Outlook/Hotmail, vào danh sách tài khoản mã hoá của profile | |

Profile được tạo trên engine Chrome vì SunBrowser của AdsPower là Chromium: trang web vẫn thấy cùng loại trình duyệt. Timezone và ngôn ngữ theo IP của proxy, như tuỳ chọn "theo IP" của AdsPower. Ở chế độ hub, việc nhập chạy trên máy của bạn qua agent, nơi có AdsPower, và mỗi profile được đưa vào nhóm hub cùng tên với nhóm AdsPower của nó (quản trị viên nhập thì nhóm được tạo; tên chỉ giữ chữ, số, `-` và `_`, nên "Ventas México" thành `Ventas-Mexico`). Nhập lại cũng đưa các profile đã nhập trước đó vào đúng nhóm.

## Cookie và fingerprint

![Cookie & fingerprint](docs/images/cookies.png)

Bấm nút cookie trên dòng profile để mở hộp thoại **Cookie & fingerprint**. **Trình duyệt của profile phải đang tắt**, vì FoxProfile mở profile ở chế độ headless để đọc và ghi cookie.

- Cookie phiên (session) khi nhập vào sẽ được đặt hạn 1 năm. Nếu không, trình duyệt sẽ xóa chúng ngay khi đóng.
- "Tạo fingerprint mới" xóa fingerprint đã lưu; lần mở sau profile sẽ là một thiết bị khác. Đổi hệ điều hành của profile cũng tự sinh fingerprint mới.

## Cấu hình

Mọi cấu hình đều không bắt buộc. Muốn đổi thì sao chép `.env.example` thành `.env`.

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `FOXPROFILE_LANG` | `en` | Ngôn ngữ giao diện: `en`, `vi` hoặc `es`. Agent của hub dùng ngôn ngữ của hub nếu máy không tự đặt |
| `FOXPROFILE_PROFILES_FILE` | `profiles.json` | File lưu danh sách profile |
| `FOXPROFILE_DATA_DIR` | `camoufox_data` | Thư mục dữ liệu, mỗi profile một thư mục con |
| `FOXPROFILE_LOG_DIR` | `logs` | Thư mục log |
| `FOXPROFILE_LOG_LEVEL` | `INFO` | Mức log |
| `FOXPROFILE_PROXY_TIMEOUT` | `10` | Thời gian chờ khi kiểm tra proxy (giây) |
| `FOXPROFILE_LAUNCH_TIMEOUT` | `90` | Thời gian API chờ trình duyệt mở xong (giây) |
| `FOXPROFILE_HEADLESS` | `false` (`true` ở chế độ server) | Chạy trình duyệt ẩn: `true`, `false` hoặc `virtual` (Linux + Xvfb) |
| `FOXPROFILE_RESTORE_TABS` | `true` | Mở lại các tab của lần chạy trước |
| `FOXPROFILE_API_TOKEN` | *(trống)* | Token cho API, web panel và MCP. Bắt buộc khi API nghe ngoài `127.0.0.1` |
| `FOXPROFILE_API_HOST` | `127.0.0.1` | Địa chỉ API |
| `FOXPROFILE_API_PORT` | `8000` | Cổng API |

### Định dạng proxy

`host:port`, `http://host:port`, `https://...`, `socks4://...`, `socks5://...`, có thể kèm `user:pass@`. Dạng `host:port:user:pass` mà nhà cung cấp proxy hay đưa cũng dùng được. Để trống nghĩa là kết nối trực tiếp.

### Dữ liệu được lưu ở đâu

```text
profiles.json                                     danh sách profile (tên, engine, proxy, hệ điều hành, timezone, locale)
camoufox_data/<tên-profile>/                      dữ liệu trình duyệt: cookie, lịch sử, localStorage
camoufox_data/<tên-profile>/fingerprint.json      fingerprint thiết bị (engine Camoufox)
camoufox_data/<tên-profile>/chrome_persona.json   fingerprint thiết bị (engine Chrome)
camoufox_data/<tên-profile>/tabs.json             các tab sẽ được mở lại
logs/foxprofile_YYYYMMDD.log                      log theo ngày
```

> [!WARNING]
> `camoufox_data/` chứa cookie đăng nhập của mọi tài khoản. Không commit, không chia sẻ thư mục này.

## REST API

Tiền tố: `/api/v1`. Xem đầy đủ tại `/docs` khi ứng dụng đang chạy.

| Method | Đường dẫn | Mô tả |
| --- | --- | --- |
| `GET` | `/health` | Kiểm tra API |
| `GET` / `POST` | `/profiles` | Liệt kê / tạo profile |
| `GET` / `PATCH` / `DELETE` | `/profiles/{name}` | Xem / sửa / xóa profile (không đổi được `engine`) |
| `GET` | `/profiles/{name}/data-dir` | Đường dẫn thư mục dữ liệu của profile |
| `POST` | `/profiles/{name}/export` | Xuất profile ra ZIP |
| `POST` | `/profiles/import` | Nhập profile từ ZIP |
| `GET` | `/profiles/{name}/cookies?format=json\|netscape` | Xuất cookie |
| `POST` | `/profiles/{name}/cookies` | Nhập cookie (`{"content": "<JSON hoặc cookies.txt>"}`) |
| `GET` / `DELETE` | `/profiles/{name}/fingerprint` | Xem / tạo fingerprint mới |
| `GET` | `/profiles/{name}/ip-check` | Kiểm tra IP ra của profile: quốc gia, timezone theo Cloudflare/ipinfo/ip-api, cảnh báo lệch |
| `GET` | `/browser` | Danh sách profile đang chạy |
| `GET` | `/browser/{name}/status` | Profile có đang chạy không |
| `POST` | `/browser/{name}/launch` | Mở trình duyệt, chờ đến khi mở xong hoặc lỗi (409 nếu đang chạy hoặc đang bận) |
| `POST` | `/browser/{name}/stop` | Đóng trình duyệt |
| `POST` | `/proxy/check` | Kiểm tra proxy |
| `POST` | `/proxy/geo-check` | Như `ip-check` cho tổ hợp `{proxy, timezone, locale}` chưa lưu |
| `POST` | `/browser/{name}/page/navigate` · `back` | Mở URL trong tab đang chọn, quay lại trang trước |
| `GET` | `/browser/{name}/page/snapshot` · `text` | Cây accessibility của trang (để chọn selector), chữ trên trang |
| `POST` | `/browser/{name}/page/click` · `type` · `press` · `wait` | Click, gõ, bấm phím, chờ phần tử (selector Playwright) |
| `POST` | `/browser/{name}/page/click-at` · `keyboard` | Click theo tọa độ, gõ vào ô đang chọn |
| `GET` | `/browser/{name}/page/screenshot` | Ảnh PNG của tab (`?format=json` để nhận base64) |
| `POST` | `/browser/{name}/page/evaluate` | Chạy JavaScript |
| `GET` / `POST` | `/browser/{name}/page/tabs` | Liệt kê / mở tab mới |
| `POST` / `DELETE` | `/browser/{name}/page/tabs/{index}/select` · `/page/tabs/{index}` | Chọn / đóng tab |
| `POST` | `/browser/{name}/page/select` | Chọn một option của `<select>` |
| `POST` | `/automation/run` | Chạy các bước trên nhiều profile đang chạy |
| `GET` / `POST` / `DELETE` | `/automation/sync` · `/automation/sync/{leader}` | Liệt kê / bắt đầu / dừng đồng bộ cửa sổ |
| `GET` | `/mcp/setup?base_url=...` | Cấu hình MCP cho từng ứng dụng AI (dùng bởi hộp thoại Kết nối AI) |

Khi có `FOXPROFILE_API_TOKEN`, mọi request (trừ `/health` và `/info`) phải gửi header `Authorization: Bearer <token>`.

Ví dụ:

```bash
# Tạo profile macOS có proxy. "engine": "chrome" để dùng engine Chrome (mặc định "camoufox").
curl -X POST http://127.0.0.1:8000/api/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{"name": "tiktok-us-02", "os_type": "macos", "proxy": "socks5://user:pass@203.0.113.10:1080"}'

# Mở trình duyệt. Trả 200 khi đã mở, 502 kèm lý do nếu lỗi.
# Thêm ?wait=false để trả về ngay (202).
curl -X POST http://127.0.0.1:8000/api/v1/browser/tiktok-us-02/launch

# Xuất cookie dạng cookies.txt
curl "http://127.0.0.1:8000/api/v1/profiles/tiktok-us-02/cookies?format=netscape" -o cookies.txt
```

## Phát triển

```bash
pip install -r requirements-dev.txt
ruff check src tests chrome_engine
ruff format --check src tests chrome_engine
pytest                  # test cần trình duyệt thật sẽ tự bỏ qua nếu chưa tải
```

Cấu trúc thư mục, quy ước code và quy trình gửi PR nằm trong [CONTRIBUTING.md](CONTRIBUTING.md). Thay đổi giao diện theo [DESIGN.md](DESIGN.md) và sửa cả app desktop lẫn web panel. Lịch sử thay đổi: [CHANGELOG.md](CHANGELOG.md).

## Đóng góp

Mọi đóng góp đều được chào đón: báo lỗi, đề xuất tính năng, sửa tài liệu, dịch thuật hay viết code.

- Báo lỗi hoặc đề xuất: mở [issue](https://github.com/chienbm98/foxprofile/issues/new/choose) theo mẫu.
- Gửi code: đọc [CONTRIBUTING.md](CONTRIBUTING.md) trước khi mở Pull Request.
- Lỗ hổng bảo mật: **không** mở issue công khai, xem [SECURITY.md](SECURITY.md).
- Mọi người tham gia cần tuân theo [Quy tắc ứng xử](CODE_OF_CONDUCT.md).

Nếu FoxProfile có ích với bạn, hãy cho dự án một ⭐ trên GitHub.

## Ủng hộ

FoxProfile miễn phí, mã nguồn mở, được làm vào thời gian rảnh và sẽ luôn như vậy. Nếu nó tiết kiệm cho bạn thời gian hay chi phí, bạn có thể mời mình một ly cà phê; cà phê sẽ thành bug fix, cập nhật theo bản mới của Camoufox và Chrome, và trả lời issue nhanh hơn.

<a href="https://buymeacoffee.com/chienbm98"><img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-%E2%98%95-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy me a coffee" /></a>

## Lưu ý sử dụng

Antidetect browser là công cụ hợp pháp, thường dùng để bảo vệ quyền riêng tư, kiểm thử web, hoặc để agency quản lý tài khoản của nhiều khách hàng. Tuy vậy, dùng nhiều tài khoản trên cùng một nền tảng có thể vi phạm điều khoản của Facebook, TikTok, Google, Shopee... Bạn tự chịu trách nhiệm về cách mình sử dụng.

## Giấy phép

Phát hành theo [giấy phép MIT](LICENSE). Copyright (c) 2026 chienbm98.

## Ghi công

- Trình duyệt [Camoufox](https://github.com/daijro/camoufox) của daijro.
- [fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) của adryfish cho engine Chrome (BSD-3).
- Font [Be Vietnam Pro](https://github.com/bettergui/BeVietnamPro) và [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono), giấy phép SIL Open Font License (`src/assets/fonts/`).
