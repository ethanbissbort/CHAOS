using System.Runtime.Versioning;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Hosting.WindowsServices;

namespace Chaos.Host;

/// <summary>
/// Windows Service integration, guarded so the same assembly builds and runs on Linux.
/// </summary>
/// <remarks>
/// <para>
/// The gateway is deployed on Windows as a service, and developed and tested on
/// Linux. Rather than splitting the project across two target frameworks, the
/// Windows-only calls sit behind <see cref="OperatingSystem.IsWindows"/>, which
/// the platform-compatibility analyser understands — so the calls are legal from
/// a <c>net10.0</c> assembly and simply do not execute elsewhere.
/// </para>
/// </remarks>
public static class WindowsServiceIntegration
{
    /// <summary>
    /// The name the service is registered under
    /// (<c>sc create ChaosHost binPath= ...</c>).
    /// </summary>
    public const string ServiceName = "ChaosHost";

    /// <summary>
    /// Registers the Windows Service lifetime when running on Windows under the
    /// service control manager. A no-op everywhere else, including a Windows
    /// console session.
    /// </summary>
    /// <param name="builder">The host application builder.</param>
    public static void AddWindowsServiceIfAvailable(IHostApplicationBuilder builder)
    {
        ArgumentNullException.ThrowIfNull(builder);

        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        AddWindowsServiceCore(builder);
    }

    /// <summary>
    /// Whether this process was started by the Windows service control manager.
    /// Always false off Windows — reported as a fact, not inferred.
    /// </summary>
    /// <returns>True when running as a Windows Service.</returns>
    public static bool IsRunningAsWindowsService() =>
        OperatingSystem.IsWindows() && IsWindowsServiceCore();

    [SupportedOSPlatform("windows")]
    private static void AddWindowsServiceCore(IHostApplicationBuilder builder) =>
        builder.Services.AddWindowsService(options => options.ServiceName = ServiceName);

    [SupportedOSPlatform("windows")]
    private static bool IsWindowsServiceCore() => WindowsServiceHelpers.IsWindowsService();
}
