﻿================================================================================
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
4B. EXPORT CLASS / INTERFACE / BUSINESS OBJECT
================================================================================

  Công cụ tự nhận biết loại object theo tên, không cần chọn gì thêm:

      ZMM_CREATEPO            -> Program  (mở bằng SE38)
      ZCL_ZO_EP2_PR_DPC_EXT   -> Class    (mở bằng SE24)
      ZII_SIIS_GR_PULL_REQ    -> Interface
      ZBUS2105                -> Business Object (mở bằng SWO1)

  Nếu nhận sai, chọn tay ở ô "Object type" (mặc định: Auto-detect).

  --- CLASS (ví dụ ZCL_ZO_EP2_PR_DPC_EXT) ---

  Công cụ mở class bằng SE24, chuyển sang chế độ "Source Code-Based"
  (xem toàn bộ class trong 1 màn hình), lấy 1 lần duy nhất,
  rồi tự tách ra từng method thành từng file riêng.

  Kết quả:

      ZCL_ZO_EP2_PR_DPC_EXT\
          ZCL_ZO_EP2_PR_DPC_EXT.txt      <- toàn bộ source của class
          methods\
              PR_HEADERSET_GET_ENTITYSET.txt
              PR_ITEMSET_GET_ENTITY.txt
              #IWBEP#IF_MGW_APPL_SRV_RUNTIME~CREATE_DEEP_ENTITY.txt
              ...

  Lưu ý: dấu "/" trong tên namespace bị đổi thành "#"
  vì Windows không cho phép "/" trong tên file.

  --- CLASS ODATA (chỉ lấy Redefinitions) ---

  Với class OData (*_DPC_EXT, *_MPC_EXT), phần code tự viết nằm ở
  các method Redefinition.

  -> Tick ô "Only redefined methods" để chỉ lấy các method đã redefine,
     bỏ qua method kế thừa không sửa gì.

  --- INTERFACE (ví dụ ZII_SIIS_GR_PULL_REQ) ---

  Interface không chứa code, code nằm ở Implementing Class.
  Công cụ tự tìm class cài đặt (ZII_xxx -> ZCL_xxx) rồi lấy method của class đó.

  Kết quả:

      ZII_SIIS_GR_PULL_REQ\
          ZCL_SIIS_GR_PULL_REQ.txt
          methods\
              ZII_SIIS_GR_PULL_REQ~SIIS_GR_PULL_REQ.txt

  --- BUSINESS OBJECT (ví dụ ZBUS2105) ---

  Business Object không có source riêng. Công cụ mở SWO1, đọc tên
  chương trình cài đặt trên màn hình Basic Data rồi export chương trình đó.

  Nếu báo lỗi không đọc được tên chương trình:
  vào SWO1 -> xem mục Basic Data -> lấy tên Program,
  rồi gõ tay tên đó vào ô "Program" và export như chương trình bình thường.


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

  Object type  (Auto-detect / Program / Class / Interface / Business object)
      Mặc định Auto-detect: tự nhận theo tên object.
      Chỉ chọn tay khi công cụ nhận sai loại.

  One file per method
      Tách mỗi method của class thành 1 file .txt riêng.
      -> Nên để có tick khi export class.

  Only redefined methods
      Chỉ lấy method Redefinition (dùng cho class OData *_DPC_EXT).
      -> Bỏ tick nếu muốn lấy toàn bộ method.


================================================================================
7. LỖI THƯỜNG GẶP
================================================================================

  "No running SAP GUI found"
      -> Chưa mở SAP Logon, hoặc file .exe đang chạy quyền Administrator
         còn SAP thì không (hoặc ngược lại). Chạy cả hai cùng quyền.

  Thiếu 1 hệ thống trong danh sách "SAP session"
      (ví dụ: DE4 hiện ra nhưng DO1 không hiện)
      -> Scripting được bật RIÊNG cho từng hệ thống bằng tham số
         sapgui/user_scripting. Hệ thống nào để FALSE sẽ không hiện ra,
         dù bạn đang đăng nhập bình thường.
         Xem khung log: dòng có dấu "!!" là connection bị chặn.
         Gửi tên hệ thống đó cho team Basis để set:
             sapgui/user_scripting = TRUE   (transaction RZ11)

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

  "is an interface and has no method implementations"
      -> Không tìm được Implementing Class của interface.
         Vào SE80 xem interface -> Implementing Classes -> lấy tên class,
         rồi export trực tiếp tên class đó.

  Class export ra nhưng không có thư mục methods
      -> Không chuyển được sang chế độ "Source Code-Based".
         Mở class trong SE24, bấm nút "Source Code-Based" thủ công,
         rồi chạy lại công cụ.

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
