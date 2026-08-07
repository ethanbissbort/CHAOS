namespace Chaos.Shell.Core;

/// <summary>What has to be done to the per-user Run key.</summary>
public enum StartupRegistrationAction
{
    /// <summary>It already says what it should say.</summary>
    None = 0,

    /// <summary>Nothing is registered and the operator asked for it.</summary>
    Register = 1,

    /// <summary>A value is there but names a different executable.</summary>
    Update = 2,

    /// <summary>A value is there and the operator turned the setting off.</summary>
    Unregister = 3,
}

/// <summary>
/// The one change to make, and the sentence explaining it.
/// </summary>
/// <param name="Value">
/// What to write. Null for <see cref="StartupRegistrationAction.Unregister"/>
/// and <see cref="StartupRegistrationAction.None"/>.
/// </param>
public sealed record StartupRegistrationPlan(
    StartupRegistrationAction Action,
    string? Value,
    string Explanation);

/// <summary>
/// "Start this shell when I sign in", expressed as a plan so the decision is
/// tested and the Windows half only writes what it is told.
/// </summary>
/// <remarks>
/// <para>
/// HKEY_CURRENT_USER, never HKEY_LOCAL_MACHINE: this registers a window for one
/// person, and writing a machine-wide autorun would need administrator rights
/// for something that is purely a convenience.
/// </para>
/// <para>
/// This starts the SHELL, not the platform. The two are deliberately separate
/// settings, because "I want to see the console when I log in" and "the
/// homestead should be controlled whether or not anyone logs in" are different
/// requirements, and the second one is the Windows service's job.
/// </para>
/// </remarks>
public static class StartupRegistration
{
    /// <summary>The per-user autorun key, relative to HKEY_CURRENT_USER.</summary>
    public const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

    /// <summary>The value name this shell owns. Nothing else may be touched.</summary>
    public const string ValueName = "ProjectCHAOS.Shell";

    /// <summary>
    /// The command line to register. Quoted, because the shell is normally
    /// installed under a path with a space in it and an unquoted autorun entry
    /// is a documented way to run the wrong executable.
    /// </summary>
    public static string CommandFor(string executablePath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
        return $"\"{executablePath.Trim()}\"";
    }

    /// <summary>
    /// Decides the change.
    /// </summary>
    /// <param name="desired">What the operator asked for.</param>
    /// <param name="currentValue">What is in the Run key now, or null if nothing.</param>
    /// <param name="executablePath">This shell's executable.</param>
    public static StartupRegistrationPlan Plan(bool desired, string? currentValue, string executablePath)
    {
        var wanted = CommandFor(executablePath);
        var current = string.IsNullOrWhiteSpace(currentValue) ? null : currentValue!.Trim();

        if (!desired)
        {
            return current is null
                ? new StartupRegistrationPlan(
                    StartupRegistrationAction.None, null,
                    "This shell is not set to start when you sign in.")
                : new StartupRegistrationPlan(
                    StartupRegistrationAction.Unregister, null,
                    "This shell will no longer start when you sign in. The platform is unaffected: "
                    + "if it runs as a Windows service it keeps running regardless of who is signed in.");
        }

        if (current is null)
        {
            return new StartupRegistrationPlan(
                StartupRegistrationAction.Register, wanted,
                $"This shell will start when you sign in, from {executablePath}.");
        }

        if (string.Equals(current, wanted, StringComparison.OrdinalIgnoreCase))
        {
            return new StartupRegistrationPlan(
                StartupRegistrationAction.None, wanted,
                "This shell already starts when you sign in.");
        }

        // A stale entry from a previous install location silently starts the
        // OLD build every morning, which is a very confusing bug to be handed.
        return new StartupRegistrationPlan(
            StartupRegistrationAction.Update, wanted,
            $"Windows was set to start a different copy of this shell ({current}). "
            + $"That will be replaced with this one, {executablePath}.");
    }
}
