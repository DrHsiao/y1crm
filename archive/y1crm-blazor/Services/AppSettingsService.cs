using Microsoft.EntityFrameworkCore;
using y1crm.Models;

namespace y1crm.Services;

/// <summary>
/// 讀取 app_settings 系統參數,快取 5 分鐘;資料庫暫時無法連線時回傳預設值,
/// 讓登入頁等靜態頁面仍能正常渲染。
/// </summary>
public class AppSettingsService(IDbContextFactory<crm1Context> dbFactory)
{
    private static readonly TimeSpan CacheDuration = TimeSpan.FromMinutes(5);

    private Dictionary<string, string> cache = new();
    private DateTime loadedAtUtc = DateTime.MinValue;

    public async Task<string> GetAsync(string key, string fallback = "")
    {
        if (DateTime.UtcNow - loadedAtUtc > CacheDuration)
        {
            try
            {
                await using var db = await dbFactory.CreateDbContextAsync();
                cache = await db.AppSettings.AsNoTracking()
                    .ToDictionaryAsync(s => s.Key, s => s.Value ?? "");
                loadedAtUtc = DateTime.UtcNow;
            }
            catch
            {
                // 連線失敗時沿用舊快取(或空集合→回 fallback),下次呼叫再重試
            }
        }

        return cache.TryGetValue(key, out var value) && !string.IsNullOrWhiteSpace(value)
            ? value
            : fallback;
    }

    public Task<string> GetCompanyNameAsync() => GetAsync("company_name", "助聽器門市 CRM");

    public Task<string> GetBranchNameAsync() => GetAsync("branch_name");
}
