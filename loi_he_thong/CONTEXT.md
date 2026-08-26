# [AI CONTEXT] - LÕI HỆ THỐNG (BO NHO RAM)
    
> [!NOTE]
> - Nhiệm vụ: THE STATE MANAGER. Nơi duy nhất lưu trữ sinh mệnh của Bot.
> - Đặc tính: Dùng Lock (nếu cần) hoặc pure dict để chia sẻ RAM cho các Worker.
> - CẤM: Không chứa bất kỳ loop tải data hay logic toán học nào. File này phải nhẹ nhất có thể.