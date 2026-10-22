# Changelog

Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/); phiên bản theo [Semantic Versioning](https://semver.org/lang/vi/).

## [Unreleased]

### Thêm
- Extension của trình duyệt: mục **Extension** (thanh bên của app desktop, Settings trong web panel) để thêm extension Chrome (`.crx`, `.zip`, hoặc link/ID Chrome Web Store, tải từ Google chỉ khi bấm Thêm) và add-on Firefox (`.xpi`) cho Camoufox, rồi chọn nơi áp dụng: mọi profile cùng engine, profile của các nhóm (hub) hoặc từng profile, cộng lại, và loại trừ từng profile. Extension giữ nguyên ID trên mọi máy (`key` trong manifest Chrome, gecko ID trong manifest Firefox) nên dữ liệu của nó đi theo profile. Ở hub chỉ admin quản lý; lệnh mở profile mang theo danh sách extension và agent tải mỗi phiên bản một lần, kiểm tra SHA-256. API `/extensions` (`PUT /extensions/upload`, `POST /extensions/store`, `PATCH /extensions/{id}`, `GET /extensions/{id}/file`). Biến mới `FOXPROFILE_EXTENSIONS_FILE`, `FOXPROFILE_EXTENSIONS_DIR`.
- Liên kết với panel Army: `FOXPROFILE_ARMY_CALLBACK` bật trang "Liên kết tài khoản với Army" (`#army=<state>`), tạo key `army_key` riêng (`POST /hub/me/army-key`) và chỉ gửi nó tới địa chỉ cấu hình đó. Admin tạo trước tài khoản để giao profile: `PUT /hub/users/{user}`. Key `army_key` đọc được `GET /accounts` (không có bí mật) để panel Army hiện tài khoản của từng profile; các route tài khoản khác vẫn từ chối mọi key cá nhân.
- Nhập lại từ AdsPower: tuỳ chọn "Nhập lại các profile đã có" trong hộp thoại nhập (web panel và app desktop) làm mới cookie của các profile đã nhập trước đó (gộp với cookie hiện có), không đổi gì khác. Profile đang mở hoặc người dùng không thấy bị từ chối. API: `reimport: true` trong `POST /adspower/import`.
- Agent tự cài bộ cài mới khi phiên bản mới cần thư viện khác (`installer_needed`, chỉ khi không có profile nào mở): tải bộ cài mà panel đang cung cấp rồi chạy (Windows: `msiexec /passive`, Windows hỏi quyền quản trị, agent mới tự khởi động; macOS: mở `.pkg` trong Installer). Mỗi bộ cài chỉ chạy một lần (`installer-tried.txt`). MSI giờ cũng khởi động agent sau khi cài kiểu `/passive`.
- Trạng thái agent trong web panel: ngoài "đã kết nối" (kèm số profile đang mở) hay "chưa chạy agent", panel cho biết agent đang tải trình duyệt lần đầu (kèm %), đang cập nhật, đang mở hoặc đang lưu profile nào. Agent báo cho hub qua `POST /hub/agent/status` (cả trong lúc tải trình duyệt, trước khi poll), `/hub/me` trả về `agent_activity` và `agent_running`.
- Nhập từ AdsPower chạy nền: bấm nhập thì hộp thoại đóng, một thông báo (toast trên web panel, snack bar trong app desktop) luôn hiện tiến độ "Đang nhập i/n: <profile>" cùng số lỗi đến lúc đó, profile hiện dần trong danh sách và thông báo cuối cho biết kết quả cùng lỗi đầu tiên. Mỗi lúc chỉ một lần nhập.
- Nhập từ AdsPower giờ mang theo tài khoản nền tảng của profile (`platform`, `username`, `password`, `fakey`) vào danh sách tài khoản khi trang là Facebook, Instagram, TikTok, X, Google hoặc Outlook/Hotmail; trang khác được báo bằng cảnh báo. Rê chuột (hoặc focus bằng bàn phím) lên tên profile hiện popup các tài khoản đã lưu (web panel; tooltip trong app desktop). Nút Mở của profile đang dừng có tài khoản có thêm mũi tên: menu "Mở với tài khoản" mở profile rồi tự điền đăng nhập bằng tài khoản đã chọn. API mới `GET /accounts` trả về tài khoản (không có bí mật) của mọi profile người gọi thấy; key cá nhân bị từ chối.
- Agent bản dev để thử nhiều môi trường: `AGENT_CHANNEL=dev` trong `build-msi.sh` / `build-pkg.sh` (hoặc input `channel` của workflow) tạo bộ cài `*-dev`; `HUB_URL` không bắt buộc, menu khay có "Đổi hub…" (mở trang nhập URL với hub hiện tại rồi uỷ quyền ở hub mới) và agent nhớ hub đăng nhập gần nhất; agent dev đã cài cập nhật theo code của hub mà nó kết nối (hub dev triển khai từ nhánh `dev`) nhưng không tự chạy bộ cài mới; agent chạy từ mã nguồn không tự cập nhật. Agent thường vẫn chỉ dùng hub đã gắn sẵn.
- Tài khoản theo profile: nút chìa khoá trên mỗi dòng (app desktop và web panel) mở danh sách tài khoản Facebook, Instagram, TikTok, X, Gmail, Hotmail/Outlook (tên đăng nhập, mật khẩu, mã bí mật 2FA). Sao chép, xem mã 2FA hiện tại (RFC 6238) và "Đăng nhập" tự điền form đăng nhập trong profile đang chạy (không vượt captcha; kiểm tra trong một tab mới để các tab của profile giữ nguyên; nếu profile đã đăng nhập sẵn, trang web chuyển đi khỏi trang đăng nhập, không có gì được điền và tab đó được đóng lại). Thông tin đăng nhập chỉ được dùng khi người dùng yêu cầu; nút Mở thường và việc nhập từ AdsPower không đụng tới chúng. Lưu mã hoá Fernet trong `accounts.json` (`FOXPROFILE_ACCOUNTS_FILE`, khoá `FOXPROFILE_ACCOUNTS_KEY` hoặc `accounts.key`), tách khỏi `profiles.json` nên không nằm trong file ZIP xuất ra. API `/profiles/{name}/accounts`; key cá nhân (MCP, agent) bị từ chối, MCP không có công cụ cho tài khoản. Phụ thuộc mới được khai báo: `cryptography` (vốn đã được cài qua `mcp`), nên agent đã cài sẽ báo cần bộ cài mới.
- Sửa hàng loạt: nút "Sửa" trên thanh thao tác hàng loạt (app desktop và web panel) đổi hệ điều hành, proxy, timezone hoặc locale của mọi profile đã chọn; chỉ các trường được đánh dấu mới đổi, để trống thì bỏ proxy hoặc về tự động. Trong web panel ở chế độ hub còn đổi được người dùng (admin) và nhóm (trưởng nhóm). Dùng lại `PATCH /profiles/{name}` cho từng profile.
- Bộ cài agent gắn sẵn địa chỉ hub (build dev hay prod): `installer/windows/build-msi.sh` / `build-pkg.sh` bắt buộc `HUB_URL=http(s)://...` (workflow Agent installers: input `hub_url` hoặc biến repo `AGENT_HUB_URL`) và ghi file `HUB_URL` vào app; agent dùng nó khi không có `--hub` hay `FOXPROFILE_HUB_URL`.
- Web panel (chế độ hub): nút "Tải agent về" ngay dưới trạng thái agent khi máy chưa kết nối, mở hộp thoại chỉ để chọn hệ điều hành (Windows / macOS) và tải bộ cài lần đầu.
- Agent tự cập nhật từ hub: agent cài bằng MSI/pkg báo phiên bản code của mình mỗi lần poll; khi hub chạy code khác và không còn profile nào mở (hoặc admin bấm "Cập nhật ngay"), agent tải code của hub (`/hub/agent/code`), giải nén vào thư mục dữ liệu (`agent-code/`) và khởi động lại. Chỉ code được cập nhật; khi `requirements.txt` đổi, agent báo cần bộ cài mới. Mục Cài đặt › Agent trong panel: tải bộ cài Windows/macOS (không cần đăng nhập), admin tải bộ cài lên (`/hub/installers/{windows,macos}`) và xem trạng thái agent của từng máy. Bộ cài thêm file `BUILD`, `requirements.txt` và hook `.pth` (`installer/agent_update_hook.py`). Cấu hình nginx mẫu thêm `location` cho việc tải bộ cài lên.
- Bộ cài agent có thể lưu trong bucket S3 riêng tư (RustFS, MinIO, AWS; `FOXPROFILE_S3_*`), trong `fox-profiles/<dev|prod>/installers/<system>/` (`FOXPROFILE_S3_ENV`) để dùng chung bucket với app khác: hub tạo URL ký sẵn (presigned) để trình duyệt tải lên theo từng phần 8 MB (multipart, vì proxy như Cloudflare chặn request trên 100 MB hoặc chờ quá 100 giây (lỗi 524): `POST /hub/installers/{system}/upload-url`, rồi `/uploaded` hoặc `/abort`) và tải về (chuyển hướng tới URL ký sẵn 15 phút) trực tiếp với bucket. Phụ thuộc mới chỉ cho hub: `boto3` trong `requirements-hub.txt` (đã có trong image Docker), không thêm vào bộ cài agent.
- Chạy bước và đồng bộ cửa sổ (RPA đơn giản): nút "Run steps" chạy một script ngắn (navigate, click, type, select, press, wait, sleep…) song song trên các profile đang chạy đã chọn; nút "Sync" lặp lại thao tác ở profile chính (click, chữ đã gõ, select, Enter/Tab/Escape, địa chỉ) sang tab đang mở của các profile khác khi chúng ở cùng trang web. Có trong app desktop, web panel, API (`/automation/run`, `/automation/sync`) và MCP (`run_steps`). Thêm hành động trang `select` (`/browser/{name}/page/select`).
- Chế độ hub (`FOXPROFILE_HUB=true`, `Dockerfile`, `compose.yaml`): server trung tâm lưu profile và tài khoản người dùng, không chạy trình duyệt. Mỗi nhân viên đăng nhập bằng tài khoản riêng, chỉ thấy profile được giao; profile mở trên máy của họ qua agent `python -m src.agent` (`foxprofile-agent.bat` / `.command`), dữ liệu được tải về khi mở và đồng bộ lại khi đóng. Xem màn hình từ xa, xuất/nhập cookie và MCP (key cá nhân cho từng người) chạy qua agent trên máy của nhân viên. Xem `docs/HUB.md`.
- Web panel: trang Settings → General (giao diện Sáng / Tối / Hệ thống và ngôn ngữ, lưu theo trình duyệt) và giao diện tối "night paper" theo cùng hệ thống thiết kế.
- Tiếng Tây Ban Nha (`es`) là ngôn ngữ thứ ba trong web panel, app desktop, thông báo lỗi của API và agent. Web panel mặc định tiếng Tây Ban Nha và chọn ngôn ngữ trong Settings → General (Settings giờ dành cho mọi người dùng). Lỗi API theo `Accept-Language` của yêu cầu, agent dùng ngôn ngữ của hub (`/info` trả về `language`).
- Chế độ hub: nhóm profile. Admin tạo nhóm và thêm người dùng (thành viên hoặc trưởng nhóm); mỗi profile thuộc tối đa một nhóm, và mọi thành viên thấy và mở được profile của nhóm trên máy của họ (vẫn chỉ một người mở cùng lúc). Trưởng nhóm thêm/bớt thành viên và chuyển profile vào nhóm của mình. Web panel có bộ chọn nhóm kèm ô tìm kiếm (kết hợp với các bộ lọc trạng thái) và API `/hub/groups`, `/hub/profile-group/{name}`.
- Bộ cài MSI cho agent trên Windows (`installer/windows/build-msi.sh`, workflow `Agent MSI`): kèm Python embeddable và mọi thư viện, cài vào Program Files và tự chạy agent mỗi khi người dùng đăng nhập Windows; dữ liệu và `.env` của từng người nằm trong `%LOCALAPPDATA%\FoxProfile`.
- Agent của hub đăng nhập qua trình duyệt: agent mở panel, nhân viên đăng nhập (hoặc dùng phiên đang mở) và bấm "Cấp quyền" trên trang "Cấp quyền đăng nhập cho agent" (có Đổi tài khoản / Huỷ). Agent nhận key riêng (`POST /hub/me/agent-key`, lưu trong `agent.json`), không còn hỏi mật khẩu trong console. Agent chạy nền với biểu tượng ở khay hệ thống (Windows) / thanh menu (macOS): trạng thái, Đăng nhập / Đổi tài khoản, Mở panel, Xem log (mở cửa sổ riêng theo dõi log, `src/agent_logs.py`, Tkinter; MSI kèm Tcl/Tk của python.org). Mở lại bộ cài sẽ cập nhật bản đang cài: MSI và pkg đóng agent đang chạy (cùng các trình duyệt nó mở) trước khi chép file, nên không cần khởi động lại máy; pkg xoá app cũ trước khi cài để không còn file của bản trước và Thoát. Bộ cài MSI chạy agent bằng `pythonw` (không có cửa sổ console). Thêm phụ thuộc `pystray` (LGPL-3.0) và `Pillow` trên Windows và macOS.
- Bộ cài `.pkg` cho agent trên macOS (`installer/macos/build-pkg.sh`, workflow `Agent installers`, trước đây là `Agent MSI`): cài "FoxProfile Agent.app" vào Applications, kèm Python (python-build-standalone) và mọi thư viện, cùng một LaunchAgent để tự chạy agent mỗi khi đăng nhập; dữ liệu của từng người nằm trong `~/Library/Application Support/FoxProfile`. Chỉ hỗ trợ Apple silicon với macOS 15 trở lên (thư viện `indexed_zstd` mà fpgen dùng không có bản chạy được cho Intel hay macOS cũ hơn). Trang cấp quyền hiển thị tên máy Mac thay vì hostname.
- Nhập từ AdsPower (web panel: Settings; app desktop: nút "Từ AdsPower"): đọc Local API của AdsPower trên cùng máy (`POST /adspower/profiles`, `POST /adspower/import`; ở chế độ hub chạy qua agent) và tạo profile engine Chrome giữ tên, proxy (kể cả proxy trong danh sách của AdsPower), đúng User-Agent và cookie. Nhập lại chỉ thêm profile chưa có. Danh sách nhóm theo nhóm của AdsPower (số đã nhập / còn lại / lỗi cho từng nhóm, chọn cả nhóm); ở chế độ hub mỗi profile vào nhóm hub cùng tên (tạo khi quản trị viên nhập).
- Agent tải Camoufox (~1,3 GB) song song với bước đăng nhập qua trình duyệt, hiện phần trăm ở biểu tượng khay và báo khi xong. Bộ cài MSI chạy agent ngay khi cài xong (cài tương tác), giống pkg macOS.
- Bộ cài agent (MSI và pkg macOS) kèm sẵn trình duyệt của engine Chrome (fingerprint-chromium, BSD-3, giấy phép đi kèm), nên profile Chrome mở được ngay lần đầu thay vì tải ~140-190 MB lúc đó (ở chế độ hub, lần tải này có thể vượt thời gian chờ mở profile).
- Engine Chrome: hệ điều hành `android` (persona điện thoại: User-Agent, client hints, `navigator.platform` `Linux armv81`, màn hình điện thoại, cảm ứng), dùng cho profile chuyển từ AdsPower. Giới hạn: font và GPU desktop, `navigator.platform` trong worker là của máy.
- Chế độ hub: "Check proxy" và "Check IP" chạy trên máy của người dùng qua agent (nơi trình duyệt dùng proxy), nên kiểm tra được cả proxy chỉ truy cập được từ máy đó, ví dụ địa chỉ Tailscale.

### Thay đổi
- Chế độ hub: thêm `compose.dokploy.yaml` để triển khai trên Dokploy (dữ liệu trong volume có tên `foxprofile-data`, tên miền và HTTPS cấu hình trong tab Domains của Dokploy). Xem `docs/HUB.md`.
- Chế độ hub: đăng nhập bằng email và mật khẩu của nền tảng định danh nội bộ (`FOXPROFILE_AUTH_URL`, `FOXPROFILE_AUTH_ORIGIN`): hub gửi `POST /auth/login` kèm header `Origin` và mở phiên riêng khi nền tảng chấp nhận. Hub không còn lưu mật khẩu; tài khoản cục bộ tự tạo ở lần đăng nhập đầu (theo email) và chỉ giữ vai trò, profile được giao, nhóm và key. Vai trò admin theo quyền `FOXPROFILE_AUTH_ADMIN_PERMISSION` (mặc định `foxprofile:admin`) trên nền tảng. Bỏ `FOXPROFILE_ADMIN_PASSWORD`, `POST /hub/users` và `PATCH /hub/users/{user}`; `POST /hub/login` nhận `{email, password}`.
- Xuất profile ZIP bỏ qua thư mục cache của trình duyệt.

### Sửa
- Profile Android (engine Chrome): chuột giờ hoạt động như ngón tay, giống chế độ thiết bị của DevTools (`Emulation.setEmitTouchEventsForMouse`). Trang web di động chờ cử chỉ chạm mà chuột không gửi được: nhấn giữ nút Thích trên m.facebook.com giờ mở bảng cảm xúc, kéo để cuộn như trên điện thoại. Tự động hoá (click, click theo toạ độ của màn hình từ xa, Sync, Run steps) trên các profile này dùng thao tác chạm (`tap`), vì chuột của Playwright bị treo khi chuột đã thành ngón tay.
- Agent macOS: thoát agent từ menu trên thanh menu rồi mở lại app (Finder, Launchpad) thì agent chạy nhưng không có biểu tượng trên thanh menu, nên không dùng được. Mở từ Finder, app giờ nhờ LaunchAgent khởi động agent (`launchctl kickstart`), giống lúc đăng nhập; nếu LaunchAgent chưa được nạp thì chạy agent như trước. Cần bộ cài `.pkg` mới, vì cập nhật code từ hub không thay script khởi động của app.
- Chế độ hub: profile mở trên máy khác không còn mất phiên đăng nhập. Khi đóng, agent xuất cookie dạng JSON nén gzip kèm SHA-256 (`cookies.json.gz`, `cookies.json.gz.sha256`) rồi mới tải lên; máy mở sau kiểm tra hash và nạp lại cookie vào trình duyệt (cơ sở dữ liệu cookie của Chrome được mã hoá theo từng máy). Cả hai chiều tải ZIP đều kèm `X-Content-SHA256`; hub từ chối (422) bản tải lên không khớp hoặc có cookie hỏng, agent từ chối mở bản tải về không khớp. Agent tải profile lên theo từng phần 8 MB (`PUT /hub/bundle/{name}/part`, rồi `?chunked=1` để ghép và kiểm tra SHA-256), vì Cloudflare chặn request trên 100 MB hoặc quá 100 giây. Tải về khi mở cũng theo từng phần: hub tạo ZIP ở nền (`POST /hub/bundle/{name}/download`), agent hỏi đến khi xong rồi tải từng phần 8 MB và kiểm tra SHA-256. Dừng profile giờ đóng trình duyệt như khi đóng cửa sổ (hành động `quit`) trước khi buộc tắt, vì trên Windows `terminate()` giết tiến trình trước khi cookie được ghi xuống đĩa.
- Xuất/nhập cookie của profile Chrome trên máy chưa tải engine Chrome giờ tự tải engine trước (trước đây báo lỗi).
- File `.env` được tìm từ thư mục làm việc thay vì thư mục chứa mã nguồn.
- Chế độ hub: lệnh gửi tới agent (mở profile, điều khiển trang…) không còn bị mất khi agent khởi động lại hoặc rớt mạng; trước đây yêu cầu chờ cũ của agent đã đóng vẫn có thể nhận lệnh và profile bị kẹt ở trạng thái "Running".
- Trình duyệt đóng giữa chừng một thao tác điều khiển trang trả về lỗi 504 thay vì lỗi 500 (ở chế độ hub, agent không còn bị treo luồng và hub chờ tới hết thời gian).
- "Check proxy" hoạt động với proxy SOCKS5/SOCKS4 (trước đây luôn báo lỗi vì gửi HTTP tới proxy SOCKS).
- Engine Camoufox mở được profile dùng proxy SOCKS5 có user/pass (Firefox không tự xác thực SOCKS5; giờ đi qua cầu nối loopback như engine Chrome).
- Agent và MCP gửi User-Agent riêng, nên dùng được hub sau Cloudflare (Browser Integrity Check chặn `Python-urllib` với lỗi 1010). Cổng của hub trong `compose.yaml` đổi được bằng `FOXPROFILE_HUB_PORT`.
- App desktop: nút "Launch" không còn bị gãy chữ thành hai dòng khi dùng giao diện English.
- Proxy dạng `host:port:user:pass` có `@` hoặc `:` trong mật khẩu không còn bị bỏ qua (trước đây profile mở mà không qua proxy). Chấp nhận proxy IPv6 dạng `[2001:db8::1]`; host proxy tối đa 253 ký tự.
- Tên profile không phân biệt hoa thường: "Demo" và "demo" không còn tạo được hai profile dùng chung một thư mục dữ liệu trên Windows/macOS. Đổi tên chỉ khác hoa thường vẫn giữ nguyên dữ liệu; tên chứa ký tự điều khiển bị từ chối.
- Đổi tên profile sang tên có thư mục dữ liệu sót lại trả về lỗi 409 thay vì làm hỏng danh sách profile. `profiles.json` được ghi nguyên tử, và các thao tác đồng thời (API, MCP, app) không còn ghi đè lẫn nhau, gây lỗi 500 hay xoá nhầm thư mục của profile vừa tạo lại.
- Proxy không vào được mạng báo lỗi dễ hiểu ("no internet connection through proxy host:port") thay vì chuỗi lỗi nội bộ; chi tiết vẫn có trong log.
- MCP `update_profile(proxy="")` xoá được proxy.
- Điều khiển trang từ chối cả scheme bị mã hoá phần trăm (`%66ile:`) và URL hỏng như `[::1`.
- Web panel: xác nhận "New fingerprint" không còn đóng luôn hộp thoại cookie; Esc đóng màn hình xem trực tiếp và đúng hộp thoại trên cùng; hiện lỗi kèm nút Thử lại khi không kết nối được server thay vì trang trắng; nút Lưu và các thao tác cookie bị khoá khi đang chạy; lỗi 422 hiển thị dễ đọc.
- App desktop: hỏi xác nhận trước khi tạo fingerprint mới; ô chọn ở tiêu đề bảng không còn bỏ chọn các profile ở trang khác; rail hiện số profile và số trình duyệt đang chạy; hộp thoại xoá nói rõ cookie và dữ liệu sẽ mất vĩnh viễn.

### Thay đổi
- Ngôn ngữ giao diện mặc định của app desktop và web panel là English; đặt `FOXPROFILE_LANG=vi` (hoặc bấm "Tiếng Việt" trong app) để dùng tiếng Việt.

## [2.3.1] - 2026-10-09

### Sửa
- Web panel: bảng profile không còn tràn mất cột thao tác (Mở/Dừng) khi cửa sổ trình duyệt hẹp hơn khoảng 1460px; bảng giờ đổi bố cục theo bề rộng thực của chính nó.
- Profile không còn kẹt ở trạng thái "Đang mở" trên app desktop khi cùng lúc được mở qua API hoặc MCP; hai lệnh mở đồng thời cho cùng một profile không còn khởi chạy hai trình duyệt.

## [2.3.0] - 2026-10-09

### Thay đổi
- Thiết kế lại giao diện app desktop và web panel theo một hệ thống chung (xem `DESIGN.md`): nền giấy sáng, rail xanh, bảng profile thẳng cột với cột Thiết bị / Proxy / Timezone · Locale, và "dấu" trạng thái (đang chạy, đang mở, lỗi) phân biệt bằng cả hình dạng lẫn màu. Font Be Vietnam Pro và JetBrains Mono được đóng gói sẵn (OFL), chạy được không cần mạng.
- Thêm bộ lọc profile kèm số đếm (tất cả, đang chạy, có/không proxy, engine Chrome) trên cả hai giao diện; web panel có thêm thao tác hàng loạt và hộp thoại xác nhận riêng; app desktop đổi được Tiếng Việt / English ngay khi đang chạy.
- Thuật ngữ chuyên ngành giữ tiếng Anh trong giao diện tiếng Việt (fingerprint, engine, timezone, locale).

### Thêm
- Nhân **Chrome (thử nghiệm)** bên cạnh Camoufox, dựa trên fingerprint-chromium: chọn khi tạo profile trên app, web panel, REST API (`"engine": "chrome"`) và MCP (`create_profile(engine="chrome")`). Nhân cố định sau khi tạo. Cookie, fingerprint, điều khiển trang, mở lại tab và Kiểm tra IP dùng được như profile Camoufox. Bản trình duyệt được tự tải ở nền khi tạo profile Chrome đầu tiên, hoặc lúc mở profile nếu chưa có (cũng tải tay được bằng `python -m chrome_engine fetch`).
- Proxy dạng `host:port:user:pass` (định dạng thường gặp của nhà bán proxy) được chấp nhận ở mọi nơi nhập proxy, kể cả có tiền tố `socks5://`.
- Mở lại các tab đang mở khi profile được dừng (lưu vào `camoufox_data/<profile>/tabs.json` mỗi 2 giây khi tab thay đổi, chỉ `http`/`https`, tối đa 30 tab). Tắt bằng `FOXPROFILE_RESTORE_TABS=false`.

### Sửa
- Rò rỉ DNS khi dùng proxy `socks5://`/`socks4://`: việc tra IP đi ra lúc mở profile và nút **Kiểm tra IP** từng phân giải tên miền bằng DNS của máy (lộ nhà mạng thật). Nay chuyển sang `socks5h://`/`socks4a://` để proxy phân giải. Bản thân trình duyệt không bị ảnh hưởng.
- Chế độ `HEADLESS=virtual` (server Linux) không còn bỏ sót tiến trình Xvfb khi profile mở lỗi (ví dụ proxy không vào được) hoặc khi runner bị kill cứng/crash. Runner tự quản lý màn hình ảo và ghi lại Xvfb nó đã bật (theo PID và thời điểm khởi động, trong thư mục tạm riêng của user) để launcher, hoặc lần mở sau, tắt đúng Xvfb đó.

## [2.2.0] - 2026-10-08

### Thêm
- Đặt cố định múi giờ (IANA) và ngôn ngữ (`vi-VN`) cho từng profile; để trống vẫn tự động theo IP.
- Nút **Kiểm tra IP**: đối chiếu IP đi ra với Cloudflare, ipinfo và ip-api, cảnh báo lệch quốc gia/múi giờ, IP datacenter, proxy dual-stack và ngôn ngữ ngẫu nhiên. Có trên app, web panel, REST API (`GET /profiles/{name}/ip-check`, `POST /proxy/geo-check`) và MCP (`check_profile_ip`).
- Hướng dẫn triển khai lên server Linux (`docs/DEPLOY.md`, `deploy/`) và các script e2e thủ công (`tests/e2e/`).

## [2.1.0] - 2026-10-08

### Thêm
- MCP server (27 tool) cho AI agent, qua stdio và qua HTTP tại `/mcp`.
- Web panel có token bảo vệ, xem và điều khiển màn hình trình duyệt từ xa.
- Server mode chạy ẩn trình duyệt (`python -m src.server`).
- Hộp thoại hướng dẫn kết nối MCP cho Claude Code, Cursor, VS Code và Claude Desktop.

### Sửa
- Server mode đọc đúng `FOXPROFILE_HEADLESS` từ `.env`.

## [2.0.0] - 2026-10-08

Phát hành đầu tiên của FoxProfile: vân tay cố định theo profile, proxy riêng, xuất/nhập cookie (JSON và Netscape), REST API, giao diện tiếng Việt/tiếng Anh.

[Unreleased]: https://github.com/chienbm98/foxprofile/compare/v2.3.1...HEAD
[2.3.1]: https://github.com/chienbm98/foxprofile/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/chienbm98/foxprofile/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/chienbm98/foxprofile/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/chienbm98/foxprofile/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/chienbm98/foxprofile/releases/tag/v2.0.0
