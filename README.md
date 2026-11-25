# GTA Eco

Công cụ Windows giúp **treo máy GTA V Online tiết kiệm tài nguyên**, có giao diện, chạy ngầm ở khay hệ thống, kèm cảnh báo raid qua Telegram.

> ⚠️ **Lưu ý:** Rockstar cấm công cụ tự động hóa của bên thứ ba. App này chỉ chỉnh ở phía hệ điều hành
> (priority, CPU core, EcoQoS, RAM, cửa sổ, ghi âm âm thanh), không inject hay đọc/ghi bộ nhớ game,
> nhưng **không có gì đảm bảo tài khoản của bạn an toàn**. Tự chịu trách nhiệm khi sử dụng.
> Dự án không liên kết với Rockstar Games hay Take-Two.

## Tính năng

- **Hai chế độ:** CHƠI (không can thiệp) và TREO MÁY. Chuyển bằng nút, menu khay hệ thống hoặc phím tắt `Ctrl+Alt+F10`.
- **Tiết kiệm tài nguyên:** hạ priority, giới hạn core CPU, EcoQoS, hạ ưu tiên bộ nhớ, thu gọn RAM định kỳ.
- **Chạy ngầm:** thu nhỏ hoặc ẩn hoàn toàn cửa sổ game, tự hiện lại khi tắt chế độ treo máy.
- **Hẹn giờ:** đóng game hoặc tắt máy sau X phút.
- **Cảnh báo raid (thử nghiệm):** ghi âm âm thanh game qua VB-Cable, so khớp lời thoại của LJT với mẫu do bạn thu, gửi Telegram.
- **Giám sát:** báo Telegram khi game đóng, crash hoặc hết giờ hẹn.
- Chạy cùng Windows, khởi động ẩn, thu vào khay khi bấm X, tự lưu cấu hình.

## Cài đặt

Cần Windows 10/11 và Python 3.9+.

```bat
git clone <repo-url>
cd GTAEco
run.bat
```

Đóng gói thành `.exe` độc lập: chạy `build_exe.bat`, kết quả ở `dist\GTAEco.exe`.

## Thiết lập cảnh báo raid

1. Cài [VB-Cable]([https://vb-audio.com/Cable/](https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip)) (miễn phí).
2. Windows → Âm thanh → Volume Mixer: đặt Output của GTA V là **CABLE Input**.
3. Trong app, tab *Cảnh báo raid*: chọn thiết bị **CABLE Output**, bấm *Kiểm tra mức âm*.
4. Tạo bot Telegram qua @BotFather, dán token, nhắn 1 tin cho bot, bấm *Lấy Chat ID*, rồi *Gửi thử*.
5. Khi nghe cuộc gọi raid của LJT, chọn đoạn ghi tương ứng và bấm *Đặt làm mẫu*.

Cách nhận diện là so khớp phổ âm với mẫu bạn thu, nên có thể báo nhầm hoặc bỏ sót. Chỉnh ngưỡng bằng thanh trượt.

## Giới hạn đã biết

- Chưa kiểm chứng trên nhiều cấu hình máy; hành vi của GTA V khi cửa sổ bị ẩn có thể khác nhau.
- Token Telegram lưu dạng văn bản trong `%APPDATA%\GTAEco\config.json`.
- Không có đồng hồ ước tính giờ raid (có thể bổ sung sau).

## Cấu trúc

| File | Vai trò |
|---|---|
| `gta_eco.py` | Giao diện, khay hệ thống, tối ưu tiến trình, phím tắt |
| `raid_alert.py` | Ghi âm, nhận diện lời thoại, Telegram |
| `run.bat` / `build_exe.bat` | Cài và chạy / đóng gói exe |
