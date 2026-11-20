================================================================================
            SAP ABAP SOURCE EXPORTER - HƯỚNG DẪN SỬ DỤNG
================================================================================

Công cụ xuất toàn bộ source code ABAP (chương trình chính + tất cả include)
ra các file .txt riêng biệt, đọc trực tiếp từ màn hình SAP GUI đang mở.

Không dùng RFC. Không cài đặt gì trên hệ thống SAP.


================================================================================
1. YÊU CẦU TRƯỚC KHI CHẠY
================================================================================

  [1] SAP GUI đang mở và ĐÃ ĐĂNG NHẬP vào hệ thống cần lấy source.
      (Công cụ dùng lại phiên đăng nhập sẵn có của bạn)

  [2] Đã bật Scripting trong SAP GUI  -> xem Mục 2

  [3] Chương trình cần export KHÔNG CÒN BREAKPOINT  -> xem Mục 3

  [4] Không chạy file .exe bằng "Run as Administrator"
      nếu SAP Logon chạy ở quyền thường.
      (Hai ứng dụng khác quyền sẽ không "nhìn thấy" nhau)


================================================================================
2. CÀI ĐẶT SAP GUI (LÀM 1 LẦN DUY NHẤT)
================================================================================

  Mở SAP Logon  ->  nút menu góc trên bên trái  ->  Options...
  Vào mục:  Accessibility & Scripting  ->  Scripting

  Thiết lập đúng như sau:

      [x] Enable scripting                               <- TICK
      [ ] Notify when a script attaches to SAP GUI       <- BỎ TICK
      [ ] Notify when a script opens a connection        <- BỎ TICK
      [ ] Show native Microsoft Windows dialogs          <- BỎ TICK

  Bấm Apply -> OK.

  *** QUAN TRỌNG ***
  Hai ô "Notify when..." BẮT BUỘC phải bỏ tick.
  Nếu còn bật, popup cảnh báo sẽ hiện lên và cướp focus khỏi editor
  giữa lúc công cụ đang copy source  ->  export sẽ bị lỗi hoặc thiếu.

  Nếu sau khi bật vẫn báo lỗi "scripting engine is not reachable":
  nhờ team Basis set tham số hệ thống:   sapgui/user_scripting = TRUE
  (Chỉ cần quyền đọc, không ảnh hưởng bảo mật)


================================================================================
3. XÓA BREAKPOINT TRƯỚC KHI EXPORT
================================================================================

  Nếu chương trình còn breakpoint, SAP có thể nhảy vào màn hình Debugger
  và công cụ sẽ DỪNG LẠI ngay lập tức (để an toàn).

  Cách xóa:  vào SE38 / SE80  ->  menu  Utilities  ->  Breakpoints
             ->  Delete  ->  All

  Lưu ý: Công cụ luôn mở chương trình ở chế độ DISPLAY (chỉ xem),
  không bao giờ sửa, không khóa object, không chạy chương trình.


================================================================================
4. CÁC BƯỚC EXPORT SOURCE
================================================================================

  BƯỚC 1 - Trên SAP GUI:
           Mở SE80 (hoặc SE38) và chọn chương trình cần lấy source.
           Ví dụ: ZMM_CREATEPO

  BƯỚC 2 - Mở file:  sap-abap-exporter.exe
           Lần đầu Windows báo "Windows protected your PC"
           ->  bấm "More info"  ->  "Run anyway"

  BƯỚC 3 - Ở mục "SAP session":
           Chọn đúng phiên SAP đang làm việc.
           (Nếu chưa thấy, bấm nút "Refresh")

  BƯỚC 4 - Bấm nút "Detect from SAP"
           Tên chương trình sẽ tự động hiện ra ở ô "Program".
           ->  KIỂM TRA LẠI tên có đúng không, nếu sai thì gõ tay.

  BƯỚC 5 - Bấm "Browse..." để chọn thư mục lưu kết quả.

  BƯỚC 6 - Bấm "Export source" và CHỜ.

           *** TRONG LÚC CHẠY: KHÔNG dùng chuột và bàn phím,
               KHÔNG chuyển qua cửa sổ khác. ***
               Công cụ cần giữ focus trên cửa sổ SAP để copy source.

  BƯỚC 7 - Xong, bấm "Open folder" để mở thư mục kết quả.


================================================================================
5. KẾT QUẢ NHẬN ĐƯỢC
================================================================================

  Công cụ tự tạo 1 thư mục trùng tên chương trình chính:

      ZMM_CREATEPO\
          ZMM_CREATEPO.txt            <- chương trình chính
          ZMM_CREATEPO_TOP.txt        <- các include
          ZMM_CREATEPO_F01.txt
          ZMM_CREATEPO_O01.txt
          ZMM_CREATEPO_I01.txt
          _export_report.txt          <- báo cáo: lấy được gì, bao nhiêu dòng
          _export_manifest.json

  Mỗi object = 1 file .txt riêng, giữ nguyên tên gốc trên SAP
  và giữ nguyên định dạng source code (thụt dòng, comment...).

  ** NÊN KIỂM TRA FILE  _export_report.txt  SAU MỖI LẦN CHẠY **
  File này liệt kê: object nào lấy thành công, bao nhiêu dòng,
  và object nào bị lỗi (nếu có).


================================================================================
6. TÙY CHỌN (OPTIONS)
================================================================================

  Only customer objects (Z*, Y*, /namespace/)
      Chỉ lấy include của khách hàng, bỏ qua include chuẩn của SAP.
      -> Tick vào nếu chỉ cần code custom, sẽ chạy nhanh hơn.

  Also read the SE80 'Includes' tree
      Đọc thêm danh sách include từ cây thư mục SE80.
      -> Nên để mặc định (có tick) cho đầy đủ.

  Write export report
      Tạo file báo cáo _export_report.txt.  -> Nên để có tick.

  Allow clipboard fallback
      *** BẮT BUỘC TICK ***
      Với ABAP Editor mới, đây là cách duy nhất đọc được source.

  Allow SAP download fallback
      Phương án dự phòng cuối cùng.  -> Nên để có tick.


================================================================================
7. LỖI THƯỜNG GẶP
================================================================================

  "No running SAP GUI found"
      -> Chưa mở SAP Logon, hoặc file .exe đang chạy quyền Administrator
         còn SAP thì không (hoặc ngược lại). Chạy cả hai cùng quyền.

  "SAP GUI is running but the scripting engine is not reachable"
      -> Chưa bật Enable scripting. Xem lại Mục 2.
         Nếu bật rồi vẫn lỗi -> nhờ Basis set sapgui/user_scripting = TRUE

  "The ABAP Debugger is open"
      -> Chương trình đang chạy trong Debugger.
         Thoát Debugger bằng  Debugger > Exit  (hoặc Shift+F3).
         KHÔNG bấm F8 (sẽ chạy tiếp chương trình).
         Sau đó xóa breakpoint theo Mục 3 rồi chạy lại.

  Tên chương trình detect sai
      -> Gõ tay tên đúng vào ô "Program" rồi bấm "Export source".
         Không cần bấm "Detect from SAP" lại.

  File .txt chỉ có 1 dòng là "SAPGUI.AbapEditor.1"
      -> Bạn đang dùng bản cũ (v1.0.0). Cập nhật lên bản mới nhất.

  Export bị thiếu dòng / file rỗng
      -> Do bị mất focus giữa chừng.
         Kiểm tra đã bỏ tick 2 ô "Notify when..." chưa (Mục 2),
         và không dùng chuột/bàn phím trong lúc chạy.


================================================================================
8. LƯU Ý AN TOÀN
================================================================================

  - Công cụ CHỈ ĐỌC, không bao giờ ghi ngược lên hệ thống SAP.
  - Luôn mở object ở chế độ Display -> không khóa object,
    đồng nghiệp vẫn sửa bình thường trong lúc bạn export.
  - Không bao giờ bấm nút "Execute" hay phím F8.
  - Nếu phát hiện màn hình Debugger, công cụ dừng lại ngay,
    không thao tác tiếp trên màn hình đang chạy chương trình.
  - Hộp thoại hỏi "Save changes?" luôn được trả lời "No".

================================================================================
