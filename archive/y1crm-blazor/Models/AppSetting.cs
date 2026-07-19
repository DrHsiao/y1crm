#nullable disable
using System;

namespace y1crm.Models;

/// <summary>
/// 系統參數檔
/// </summary>
public partial class AppSetting
{
    /// <summary>
    /// 參數鍵
    /// </summary>
    public string Key { get; set; }

    /// <summary>
    /// 參數值
    /// </summary>
    public string Value { get; set; }

    /// <summary>
    /// 參數說明
    /// </summary>
    public string Description { get; set; }

    public DateTime UpdatedAt { get; set; }
}
