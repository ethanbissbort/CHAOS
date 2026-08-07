namespace Chaos.Shell.Core;

/// <summary>
/// What saving a settings edit will and will not do, in the operator's words.
/// </summary>
/// <param name="RequiresPlatformRestart">
/// The platform has to be restarted before the change takes effect. The
/// settings page offers to do it rather than leaving the operator to guess.
/// </param>
/// <param name="RequiresReconnect">The shell will point itself somewhere else.</param>
/// <param name="Notes">
/// One line per change, saying what happens. Ordered as the fields appear on
/// the page.
/// </param>
/// <param name="Warnings">
/// Changes that will NOT take effect, and why. These matter more than the notes:
/// a setting that looks saved but is ignored is worse than one that was refused.
/// </param>
public sealed record SettingsImpact(
    bool RequiresPlatformRestart,
    bool RequiresReconnect,
    IReadOnlyList<string> Notes,
    IReadOnlyList<string> Warnings)
{
    public static readonly SettingsImpact Nothing =
        new(false, false, Array.Empty<string>(), Array.Empty<string>());

    public bool AnythingChanged => Notes.Count > 0 || Warnings.Count > 0;

    /// <summary>The line the Save button's confirmation leads with.</summary>
    public string Headline =>
        RequiresPlatformRestart
            ? "Saving this needs the platform restarted before it takes effect."
            : RequiresReconnect
                ? "Saved. The shell will reconnect at the new address."
                : AnythingChanged
                    ? "Saved."
                    : "Nothing changed.";
}

/// <summary>
/// Works out the consequences of a settings edit.
/// </summary>
/// <remarks>
/// The awkward truth this encodes: the shell can only apply storage settings to
/// a platform it starts itself. A Windows service reads its own configuration,
/// installed once by the installer, and this shell does not rewrite it. Saying
/// so out loud is the difference between an operator who moves their database
/// and one who believes they moved their database.
/// </remarks>
public static class SettingsChangeImpact
{
    public static SettingsImpact Evaluate(
        ShellSettings before,
        ShellSettings after,
        PlatformRunMode runMode)
    {
        ArgumentNullException.ThrowIfNull(before);
        ArgumentNullException.ThrowIfNull(after);

        var notes = new List<string>();
        var warnings = new List<string>();
        var restart = false;
        var reconnect = false;

        var shellStartedIt = runMode == PlatformRunMode.ManagedByThisShell;
        var service = runMode == PlatformRunMode.WindowsService;

        if (AddressChanged(before, after))
        {
            reconnect = true;
            var target = after.TryComposeGatewayUri()?.ToString() ?? "the new address";
            notes.Add($"The shell will look for the gateway at {target}.");

            if (shellStartedIt)
            {
                restart = true;
                notes.Add(
                    "The platform running under this shell was told to listen on the old address, "
                    + "so it has to be restarted to move.");
            }
            else if (service)
            {
                warnings.Add(
                    $"This only changes where the shell looks. The Windows service '{before.ServiceName}' "
                    + "keeps listening where it was configured to listen; if it is not on the new "
                    + "address, the shell will stop being able to see it.");
            }
        }

        if (!string.Equals(before.DatabasePath, after.DatabasePath, StringComparison.Ordinal))
        {
            Storage(notes, warnings, ref restart, shellStartedIt, service,
                after.ServiceName,
                after.DatabasePath is null
                    ? "The database location goes back to the platform's own default."
                    : $"The platform will use the database at {after.DatabasePath}.",
                "database location");
        }

        if (!string.Equals(before.DataDirectory, after.DataDirectory, StringComparison.Ordinal))
        {
            Storage(notes, warnings, ref restart, shellStartedIt, service,
                after.ServiceName,
                after.DataDirectory is null
                    ? "The data directory goes back to the platform's own default."
                    : $"The platform will use the data directory {after.DataDirectory}.",
                "data directory");
        }

        if (!string.Equals(before.ServiceName, after.ServiceName, StringComparison.Ordinal))
        {
            notes.Add(
                $"The shell will look for a Windows service called '{after.ServiceName}' from now on.");

            if (service)
            {
                warnings.Add(
                    $"The platform is currently being watched as '{before.ServiceName}'. Until the "
                    + "shell next checks, its service reading refers to the old name.");
            }
        }

        if (!string.Equals(before.HostExecutablePath, after.HostExecutablePath, StringComparison.Ordinal))
        {
            notes.Add(after.HostExecutablePath is null
                ? "The shell will search for Chaos.Host.exe again instead of using a fixed path."
                : $"The shell will start {after.HostExecutablePath} when it starts the platform itself.");

            if (shellStartedIt)
            {
                notes.Add("The platform already running under this shell is unaffected until it is restarted.");
            }
        }

        if (before.Theme != after.Theme)
        {
            notes.Add($"Theme: {Describe(after.Theme)}. This applies to the shell's own windows immediately.");
        }

        if (before.StartWithWindows != after.StartWithWindows)
        {
            notes.Add(after.StartWithWindows
                ? "This shell will open when you sign in to Windows. That opens the window, not the "
                + "platform — see 'Start the platform when the shell opens' for that."
                : "This shell will no longer open when you sign in to Windows.");
        }

        if (before.AutoStartPlatform != after.AutoStartPlatform)
        {
            notes.Add(after.AutoStartPlatform
                ? "The shell will start the platform on launch if nothing is answering."
                : "The shell will no longer start the platform on launch; it will wait for you to press Start.");
        }

        if (before.PreferWindowsService != after.PreferWindowsService)
        {
            notes.Add(after.PreferWindowsService
                ? "When both are available, the shell will start the Windows service, which keeps "
                + "running after the shell closes."
                : "The shell will start the platform itself even when the Windows service is installed. "
                + "A platform started that way STOPS when this shell exits.");
        }

        if (!string.Equals(before.AnnunciatorMonitor, after.AnnunciatorMonitor, StringComparison.Ordinal)
            || before.AnnunciatorFullScreen != after.AnnunciatorFullScreen)
        {
            notes.Add(
                $"The annunciator will open on {DescribeMonitor(after.AnnunciatorMonitor)}"
                + (after.AnnunciatorFullScreen ? ", full-screen." : ".")
                + " Close and reopen the panel to move one that is already showing.");
        }

        return new SettingsImpact(restart, reconnect, notes, warnings);
    }

    private static void Storage(
        List<string> notes,
        List<string> warnings,
        ref bool restart,
        bool shellStartedIt,
        bool service,
        string serviceName,
        string note,
        string what)
    {
        notes.Add(note);

        if (shellStartedIt)
        {
            restart = true;
            notes.Add($"The platform has to be restarted to pick up the new {what}.");
            return;
        }

        if (service)
        {
            warnings.Add(
                $"The {what} here is only applied to a platform this shell starts itself. The Windows "
                + $"service '{serviceName}' reads its own configuration, which this shell does not "
                + $"change — its {what} is unaffected.");
            return;
        }

        notes.Add($"This takes effect the next time this shell starts the platform itself.");
    }

    private static bool AddressChanged(ShellSettings before, ShellSettings after) =>
        !string.Equals(before.HostAddress, after.HostAddress, StringComparison.OrdinalIgnoreCase)
        || before.HostPort != after.HostPort
        || before.UseHttps != after.UseHttps;

    private static string Describe(ShellTheme theme) => theme switch
    {
        ShellTheme.Dark => "always dark",
        ShellTheme.Light => "always light",
        _ => "follow Windows",
    };

    private static string DescribeMonitor(string? monitor) =>
        string.IsNullOrWhiteSpace(monitor) || string.Equals(monitor, "primary", StringComparison.OrdinalIgnoreCase)
            ? "the primary display"
            : $"display '{monitor}'";
}
