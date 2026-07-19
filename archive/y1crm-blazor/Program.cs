using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.Authorization;
using System.Globalization;
using Microsoft.EntityFrameworkCore;
using Radzen;
using y1crm.Components;
using y1crm.Models;
using y1crm.Services;

CultureInfo.DefaultThreadCurrentCulture = new CultureInfo("zh-TW");
CultureInfo.DefaultThreadCurrentUICulture = new CultureInfo("zh-TW");

var builder = WebApplication.CreateBuilder(args);

// Add services to the container.
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

builder.Services.AddCascadingAuthenticationState();

builder.Services.AddRadzenComponents();

builder.Services.AddSingleton<AppSettingsService>();

builder.Services.AddAuthentication(CookieAuthenticationDefaults.AuthenticationScheme)
    .AddCookie(options =>
    {
        options.LoginPath = "/login";
        options.AccessDeniedPath = "/login";
    });

builder.Services.AddAuthorization(options =>
{
    options.FallbackPolicy = new AuthorizationPolicyBuilder()
        .RequireAuthenticatedUser()
        .Build();
});

var connectionString = builder.Configuration.GetConnectionString("crm1")
    ?? throw new InvalidOperationException("Connection string 'crm1' not found.");

// 同時註冊 factory(互動元件每次操作建短命 context)與 scoped context(靜態 SSR 頁面直接注入)
builder.Services.AddDbContextFactory<crm1Context>(options =>
    options.UseMySql(connectionString, new MySqlServerVersion(new Version(8, 0, 46)),
        mySqlOptions => mySqlOptions.EnableRetryOnFailure()));

var app = builder.Build();

// Configure the HTTP request pipeline.
if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/Error", createScopeForErrors: true);
    // The default HSTS value is 30 days. You may want to change this for production scenarios, see https://aka.ms/aspnetcore-hsts.
    app.UseHsts();
}

app.UseHttpsRedirection();

app.UseStaticFiles();

app.UseAuthentication();
app.UseAuthorization();

app.UseAntiforgery();

app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.MapPost("/logout", async (HttpContext ctx) =>
{
    await ctx.SignOutAsync(CookieAuthenticationDefaults.AuthenticationScheme);
    return Results.Redirect("/login");
});

// 明/暗主題切換:翻轉 cookie 後導回原頁(App.razor 依此 cookie 決定載入的 Radzen 主題)
app.MapPost("/toggle-theme", (HttpContext ctx) =>
{
    var next = ctx.Request.Cookies["y1crm-theme"] == "dark" ? "light" : "dark";
    ctx.Response.Cookies.Append("y1crm-theme", next, new CookieOptions
    {
        Path = "/",
        Expires = DateTimeOffset.UtcNow.AddYears(1),
        IsEssential = true,
        SameSite = SameSiteMode.Lax,
    });

    var referer = ctx.Request.Headers.Referer.ToString();
    var localPath = Uri.TryCreate(referer, UriKind.Absolute, out var uri) ? uri.PathAndQuery : "/";
    return Results.LocalRedirect(string.IsNullOrEmpty(localPath) ? "/" : localPath);
}).AllowAnonymous();

app.Run();
