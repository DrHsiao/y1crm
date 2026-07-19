using Microsoft.EntityFrameworkCore;
using y1crm.Models;

namespace y1crm.Components.Pages.Customers;

/// <summary>
/// 客戶「輸入/查詢」共用表單模型。
/// 空字串 = 未填(查詢時代表不限);三態選項用 "" / "y" / "n" 表示 不限 / 是 / 否,
/// 因為實體上的 ConsentSigned、SubsidyApplied 是非空 bool,無法直接表達「不限」。
/// </summary>
public class CustomerFormModel
{
    public string CustomerCode { get; set; } = "";
    public string Name { get; set; } = "";
    public string NationalId { get; set; } = "";
    public string Gender { get; set; } = "";          // "" / "M" / "F"
    public DateOnly? BirthDate { get; set; }
    public DateOnly? RegistrationDate { get; set; }

    public string PhoneMobile { get; set; } = "";
    public string PhoneHome { get; set; } = "";
    public string Email { get; set; } = "";
    public string ContactName { get; set; } = "";
    public string ContactPhone { get; set; } = "";

    public string HouseholdCity { get; set; } = "";
    public string HouseholdDistrict { get; set; } = "";
    public string HouseholdVillage { get; set; } = "";
    public string HouseholdDetail { get; set; } = "";

    public string MailingCity { get; set; } = "";
    public string MailingDistrict { get; set; } = "";
    public string MailingVillage { get; set; } = "";
    public string MailingDetail { get; set; } = "";

    public string ConsentSel { get; set; } = "";      // "" / "y" / "n"
    public DateOnly? ConsentDate { get; set; }
    public string SubsidySel { get; set; } = "";      // "" / "y" / "n"
    public string Notes { get; set; } = "";

    /// <summary>空白字串正規化為 null(NationalId 等唯一索引欄位存空字串會撞索引)。</summary>
    public static string? Clean(string? s) => string.IsNullOrWhiteSpace(s) ? null : s.Trim();

    public static CustomerFormModel FromEntity(Customer c) => new()
    {
        CustomerCode = c.CustomerCode ?? "",
        Name = c.Name ?? "",
        NationalId = c.NationalId ?? "",
        Gender = c.Gender ?? "",
        BirthDate = c.BirthDate,
        RegistrationDate = c.RegistrationDate,
        PhoneMobile = c.PhoneMobile ?? "",
        PhoneHome = c.PhoneHome ?? "",
        Email = c.Email ?? "",
        ContactName = c.ContactName ?? "",
        ContactPhone = c.ContactPhone ?? "",
        HouseholdCity = c.HouseholdCity ?? "",
        HouseholdDistrict = c.HouseholdDistrict ?? "",
        HouseholdVillage = c.HouseholdVillage ?? "",
        HouseholdDetail = c.HouseholdDetail ?? "",
        MailingCity = c.MailingCity ?? "",
        MailingDistrict = c.MailingDistrict ?? "",
        MailingVillage = c.MailingVillage ?? "",
        MailingDetail = c.MailingDetail ?? "",
        ConsentSel = c.ConsentSigned ? "y" : "n",
        ConsentDate = c.ConsentDate,
        SubsidySel = c.SubsidyApplied ? "y" : "n",
        Notes = c.Notes ?? "",
    };

    /// <summary>寫回實體。CustomerCode 僅在有填時覆蓋(新增時的自動編號由呼叫端處理)。</summary>
    public void ApplyTo(Customer c)
    {
        var code = Clean(CustomerCode);
        if (code != null)
        {
            c.CustomerCode = code;
        }

        c.Name = Clean(Name) ?? c.Name;
        c.NationalId = Clean(NationalId)?.ToUpperInvariant();
        c.Gender = Clean(Gender);
        c.BirthDate = BirthDate;
        if (RegistrationDate is { } rd)
        {
            c.RegistrationDate = rd;
        }

        c.PhoneMobile = Clean(PhoneMobile);
        c.PhoneHome = Clean(PhoneHome);
        c.Email = Clean(Email);
        c.ContactName = Clean(ContactName);
        c.ContactPhone = Clean(ContactPhone);

        c.HouseholdCity = Clean(HouseholdCity);
        c.HouseholdDistrict = Clean(HouseholdDistrict);
        c.HouseholdVillage = Clean(HouseholdVillage);
        c.HouseholdDetail = Clean(HouseholdDetail);

        c.MailingCity = Clean(MailingCity);
        c.MailingDistrict = Clean(MailingDistrict);
        c.MailingVillage = Clean(MailingVillage);
        c.MailingDetail = Clean(MailingDetail);

        c.ConsentSigned = ConsentSel == "y";
        c.ConsentDate = ConsentDate;
        if (c.ConsentSigned && c.ConsentDate is null)
        {
            c.ConsentDate = DateOnly.FromDateTime(DateTime.Today);
        }

        c.SubsidyApplied = SubsidySel == "y";
        c.Notes = Clean(Notes);
    }

    /// <summary>把資料庫唯一索引衝突轉成人看得懂的訊息。</summary>
    public static string SaveErrorMessage(DbUpdateException ex)
    {
        var msg = ex.InnerException?.Message ?? ex.Message;
        if (msg.Contains("uq_national_id"))
        {
            return "此身分證字號已有其他客戶建檔,請改用查詢開啟該客戶。";
        }

        if (msg.Contains("uq_customer_code"))
        {
            return "客戶編號重複,請改用其他編號或留空自動產生。";
        }

        return $"儲存失敗:{msg}";
    }
}
