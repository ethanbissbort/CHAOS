namespace Chaos.Shell.Core;

/// <summary>Field names the settings page can attach a message to.</summary>
public static class SettingsField
{
    public const string HostAddress = nameof(ShellSettings.HostAddress);
    public const string HostPort = nameof(ShellSettings.HostPort);
    public const string ServiceName = nameof(ShellSettings.ServiceName);
    public const string HostExecutablePath = nameof(ShellSettings.HostExecutablePath);
    public const string DatabasePath = nameof(ShellSettings.DatabasePath);
    public const string DataDirectory = nameof(ShellSettings.DataDirectory);
}

/// <summary>One rejected field, with the sentence shown beside it.</summary>
public sealed record SettingsProblem(string Field, string Message);

/// <summary>
/// Outcome of validating a settings edit.
/// </summary>
/// <param name="Normalised">
/// The settings as they would be stored: trimmed, blanks turned into null,
/// scheme lower-cased. Present even when <paramref name="Problems"/> is not
/// empty, so a page can keep showing what the operator typed.
/// </param>
/// <param name="Problems">Empty when the edit can be saved.</param>
public sealed record SettingsValidation(ShellSettings Normalised, IReadOnlyList<SettingsProblem> Problems)
{
    public bool IsValid => Problems.Count == 0;

    /// <summary>The message for one field, or null when that field is fine.</summary>
    public string? For(string field) =>
        Problems.FirstOrDefault(p => string.Equals(p.Field, field, StringComparison.Ordinal))?.Message;
}

/// <summary>
/// Validates and normalises a settings edit before it is written.
/// </summary>
/// <remarks>
/// Every rule here exists because the failure it prevents is silent. A relative
/// data directory resolves against whatever working directory the service
/// control manager happened to hand the platform, so the database ends up
/// somewhere nobody can find and yesterday's history appears to have vanished.
/// A port typo produces a shell that waits forever on an address nothing is
/// listening on. Both are caught at the point of entry, with the field named.
/// </remarks>
public static class ShellSettingsValidator
{
    /// <summary>Longest a Windows service name may be.</summary>
    private const int MaxServiceNameLength = 256;

    public static SettingsValidation Validate(ShellSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);

        var problems = new List<SettingsProblem>();

        var address = Clean(settings.HostAddress);
        var serviceName = Clean(settings.ServiceName);

        var normalised = settings with
        {
            Version = ShellSettings.CurrentVersion,
            HostAddress = address ?? string.Empty,
            ServiceName = serviceName ?? string.Empty,
            HostExecutablePath = Clean(settings.HostExecutablePath),
            DatabasePath = Clean(settings.DatabasePath),
            DataDirectory = Clean(settings.DataDirectory),
            AnnunciatorMonitor = Clean(settings.AnnunciatorMonitor),
        };

        ValidateAddress(address, problems);

        if (settings.HostPort is < 1 or > 65535)
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostPort,
                $"The port must be between 1 and 65535. '{settings.HostPort}' is not."));
        }

        ValidateServiceName(serviceName, problems);
        ValidatePath(normalised.HostExecutablePath, SettingsField.HostExecutablePath,
            "the Chaos.Host.exe location", problems);
        ValidateDatabase(normalised.DatabasePath, problems);
        ValidatePath(normalised.DataDirectory, SettingsField.DataDirectory,
            "the data directory", problems);

        // Composition is checked last so an operator does not get two messages
        // for the same mistake: the field-level rules above catch the causes.
        if (problems.Count == 0 && normalised.TryComposeGatewayUri() is null)
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostAddress,
                $"'{address}' and port {settings.HostPort} do not form an address this shell can reach."));
        }

        return new SettingsValidation(normalised, problems);
    }

    private static void ValidateAddress(string? address, List<SettingsProblem> problems)
    {
        if (address is null)
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostAddress,
                "Enter the address of the gateway — a host name such as chaos-node, "
                + $"or an IP address. Use {HostEndpoints.DefaultGatewayHost} for this machine."));
            return;
        }

        if (address.Contains("://", StringComparison.Ordinal))
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostAddress,
                "Enter the host name only, without http:// — use the HTTPS switch for the "
                + "scheme and the Port box for the port."));
            return;
        }

        if (address.Contains('/', StringComparison.Ordinal))
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostAddress,
                "Enter the host name only. The gateway is always reached at the root of "
                + "its address, so a path here would be ignored."));
            return;
        }

        // A colon inside an unbracketed value is either an IPv6 literal or
        // somebody putting the port in the wrong box. Tell them which.
        if (address.Contains(':', StringComparison.Ordinal)
            && !address.StartsWith('[')
            && !LooksLikeIpv6(address))
        {
            problems.Add(new SettingsProblem(
                SettingsField.HostAddress,
                "Put the port in the Port box, not in the address."));
        }
    }

    private static void ValidateServiceName(string? serviceName, List<SettingsProblem> problems)
    {
        if (serviceName is null)
        {
            problems.Add(new SettingsProblem(
                SettingsField.ServiceName,
                $"Enter the name of the Windows service. The installer creates "
                + $"'{ShellMessages.ServiceName}'."));
            return;
        }

        if (serviceName.Length > MaxServiceNameLength)
        {
            problems.Add(new SettingsProblem(
                SettingsField.ServiceName,
                $"A Windows service name cannot be longer than {MaxServiceNameLength} characters."));
            return;
        }

        // Documented by CreateService: forward and back slashes are illegal.
        if (serviceName.Contains('/', StringComparison.Ordinal)
            || serviceName.Contains('\\', StringComparison.Ordinal))
        {
            problems.Add(new SettingsProblem(
                SettingsField.ServiceName,
                "A Windows service name cannot contain a slash. This is the service name "
                + "(for example ChaosHost), not a path."));
        }
    }

    private static void ValidateDatabase(string? value, List<SettingsProblem> problems)
    {
        if (value is null)
        {
            return;
        }

        // A connection URL is accepted verbatim — the platform takes one, and
        // refusing it here would force an operator back to a config file.
        if (value.Contains("://", StringComparison.Ordinal))
        {
            return;
        }

        ValidatePath(value, SettingsField.DatabasePath, "the database location", problems);
    }

    private static void ValidatePath(
        string? value,
        string field,
        string description,
        List<SettingsProblem> problems)
    {
        if (value is null)
        {
            return;
        }

        if (value.AsSpan().IndexOfAny(Path.GetInvalidPathChars()) >= 0)
        {
            problems.Add(new SettingsProblem(
                field, $"That is not a usable path for {description}: it contains characters a path cannot hold."));
            return;
        }

        if (!IsRooted(value))
        {
            problems.Add(new SettingsProblem(
                field,
                $"Give a full path for {description}, starting with a drive letter or \\\\server. "
                + "A relative path is resolved against whatever directory the platform happens to be "
                + "started from, which is not somewhere you can find it again."));
        }
    }

    /// <summary>
    /// Whether a path is absolute on Windows, decided without asking the
    /// running OS — these settings are edited on Windows but validated by tests
    /// that run anywhere, and a rule that changes with the test host is not a
    /// rule.
    /// </summary>
    internal static bool IsRooted(string value)
    {
        if (value.Length >= 2 && value.StartsWith(@"\\", StringComparison.Ordinal))
        {
            return true;
        }

        if (value.Length >= 3
            && char.IsAsciiLetter(value[0])
            && value[1] == ':'
            && (value[2] == '\\' || value[2] == '/'))
        {
            return true;
        }

        // POSIX roots are accepted so the same rule can be exercised — and so a
        // path typed on a developer's Linux checkout is not rejected as relative.
        return value.StartsWith('/');
    }

    private static bool LooksLikeIpv6(string value) =>
        System.Net.IPAddress.TryParse(value, out var parsed)
        && parsed.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6;

    private static string? Clean(string? value)
    {
        var trimmed = value?.Trim();
        return string.IsNullOrEmpty(trimmed) ? null : trimmed;
    }
}
