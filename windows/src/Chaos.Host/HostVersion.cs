using System.Reflection;

namespace Chaos.Host;

/// <summary>
/// Identity of this gateway build: its own version, and the Project CHAOS
/// platform version it was contracted against.
/// </summary>
/// <remarks>
/// Both come from <c>windows/Directory.Build.props</c> — <c>Version</c> and
/// <c>ChaosPlatformVersion</c> — and are read back from assembly metadata rather
/// than restated in code, so there is exactly one source of truth.
/// </remarks>
public static class HostVersion
{
    private const string PlatformVersionMetadataKey = "ChaosPlatformVersion";

    /// <summary>This gateway's informational version, for example <c>0.5.0</c>.</summary>
    public static string Version { get; } = ReadInformationalVersion();

    /// <summary>
    /// The platform (Python API) version this host was built against, for
    /// example <c>0.4.0</c>. The route-ownership manifest describes that API
    /// surface, so a backend on a different major.minor is worth reporting.
    /// </summary>
    public static string PlatformVersionContract { get; } = ReadAssemblyMetadata(PlatformVersionMetadataKey) ?? "unknown";

    /// <summary>The .NET runtime this host is running on.</summary>
    public static string Framework { get; } = Environment.Version.ToString();

    /// <summary>
    /// Whether a backend-reported version satisfies
    /// <see cref="PlatformVersionContract"/>, compared on major.minor.
    /// </summary>
    /// <param name="backendVersion">The version string the backend reported.</param>
    /// <returns>
    /// True when major and minor match; false when they do not;
    /// <see langword="null"/> when either version cannot be parsed. Null means
    /// "unknown" and is never treated as a match.
    /// </returns>
    public static bool? IsPlatformVersionCompatible(string? backendVersion)
    {
        if (!TryParseMajorMinor(backendVersion, out var backendMajor, out var backendMinor)
            || !TryParseMajorMinor(PlatformVersionContract, out var contractMajor, out var contractMinor))
        {
            return null;
        }

        return backendMajor == contractMajor && backendMinor == contractMinor;
    }

    private static bool TryParseMajorMinor(string? value, out int major, out int minor)
    {
        major = 0;
        minor = 0;

        if (string.IsNullOrWhiteSpace(value))
        {
            return false;
        }

        var core = value.Split('+', '-')[0];
        var parts = core.Split('.');
        return parts.Length >= 2
            && int.TryParse(parts[0], out major)
            && int.TryParse(parts[1], out minor);
    }

    private static string ReadInformationalVersion()
    {
        var assembly = typeof(HostVersion).Assembly;
        var informational = assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
            .InformationalVersion;

        if (!string.IsNullOrWhiteSpace(informational))
        {
            // Strip the "+<commit>" SourceLink suffix; operators read this field.
            var plus = informational.IndexOf('+', StringComparison.Ordinal);
            return plus >= 0 ? informational[..plus] : informational;
        }

        return assembly.GetName().Version?.ToString() ?? "unknown";
    }

    private static string? ReadAssemblyMetadata(string key)
    {
        foreach (var attribute in typeof(HostVersion).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>())
        {
            if (string.Equals(attribute.Key, key, StringComparison.Ordinal)
                && !string.IsNullOrWhiteSpace(attribute.Value))
            {
                return attribute.Value;
            }
        }

        return null;
    }
}
