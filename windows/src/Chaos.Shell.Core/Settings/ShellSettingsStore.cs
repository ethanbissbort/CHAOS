using System.Text.Json;

namespace Chaos.Shell.Core;

/// <summary>Reads and writes the operator's settings.</summary>
public interface IShellSettingsStore
{
    /// <summary>
    /// Loads the settings. Never throws and never returns null: a shell that
    /// will not open because its own preferences file is damaged locks an
    /// operator out of their control system.
    /// </summary>
    SettingsLoad Load();

    /// <summary>Saves the settings. Returns the failure reason, or null on success.</summary>
    string? Save(ShellSettings settings);

    /// <summary>Where the file is, for the settings page to show.</summary>
    string Path { get; }
}

/// <summary>
/// What loading produced.
/// </summary>
/// <param name="Settings">Always usable. Defaults when nothing could be read.</param>
/// <param name="Existed">False on a genuinely first run.</param>
/// <param name="Problem">
/// Non-null when a file was there but could not be used. The launcher shows
/// this: silently reverting an operator's settings to defaults, and letting
/// them believe their configured gateway is in use, is the failure this field
/// exists to prevent.
/// </param>
public sealed record SettingsLoad(ShellSettings Settings, bool Existed, string? Problem)
{
    public static readonly SettingsLoad FirstRun = new(ShellSettings.Defaults, false, null);
}

/// <summary>
/// JSON settings store, written atomically beside the shell's other per-user
/// state.
/// </summary>
public sealed class FileShellSettingsStore : IShellSettingsStore
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
    };

    private readonly string _path;

    public FileShellSettingsStore(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        _path = path;
    }

    public string Path => _path;

    /// <summary><c>%APPDATA%\ProjectCHAOS\Shell\settings.json</c> on Windows.</summary>
    public static string DefaultPath => System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ProjectCHAOS",
        "Shell",
        "settings.json");

    public static FileShellSettingsStore Default() => new(DefaultPath);

    public SettingsLoad Load()
    {
        try
        {
            if (!File.Exists(_path))
            {
                return SettingsLoad.FirstRun;
            }

            var json = File.ReadAllText(_path);
            if (string.IsNullOrWhiteSpace(json))
            {
                return new SettingsLoad(
                    ShellSettings.Defaults, true,
                    $"The settings file at {_path} is empty, so the shell is using its defaults.");
            }

            var settings = JsonSerializer.Deserialize<ShellSettings>(json, Options);
            if (settings is null)
            {
                return new SettingsLoad(
                    ShellSettings.Defaults, true,
                    $"The settings file at {_path} did not contain settings, so the shell is using its defaults.");
            }

            if (settings.Version != ShellSettings.CurrentVersion)
            {
                // Written by a newer shell. Guessing at fields we do not
                // understand is how a gateway address silently becomes wrong.
                return new SettingsLoad(
                    ShellSettings.Defaults, true,
                    $"The settings file at {_path} was written by a different version of this shell "
                    + $"(format {settings.Version}, this shell reads {ShellSettings.CurrentVersion}). "
                    + "The shell is using its defaults rather than guessing. Save from this Settings "
                    + "page to rewrite it.");
            }

            // A file can be valid JSON and still hold a value this shell cannot
            // act on — hand back the normalised form and say what was wrong.
            var validation = ShellSettingsValidator.Validate(settings);
            if (!validation.IsValid)
            {
                var detail = string.Join(" ", validation.Problems.Select(p => p.Message));
                return new SettingsLoad(validation.Normalised, true,
                    $"Some stored settings are not usable: {detail}");
            }

            return new SettingsLoad(validation.Normalised, true, null);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return new SettingsLoad(
                ShellSettings.Defaults, true,
                $"The settings file at {_path} could not be read ({ex.Message}), so the shell is "
                + "using its defaults. Your saved settings have not been overwritten.");
        }
    }

    public string? Save(ShellSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);

        try
        {
            var directory = System.IO.Path.GetDirectoryName(_path);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            // Write-then-replace, as for the layout: a power cut during a save
            // leaves either the old settings or the new ones, never a truncated
            // file that would silently revert the gateway address to default.
            var temporary = _path + ".tmp";
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(settings with { Version = ShellSettings.CurrentVersion }, Options));
            File.Move(temporary, _path, overwrite: true);
            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return ex.Message;
        }
    }
}
