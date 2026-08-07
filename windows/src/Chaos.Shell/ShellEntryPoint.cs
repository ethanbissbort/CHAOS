using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.Windows.AppLifecycle;

namespace Chaos.Shell;

/// <summary>
/// Entry point.
///
/// Named ShellEntryPoint rather than Program on purpose: when
/// DISABLE_XAML_GENERATED_MAIN is not in force, the XAML compiler emits its own
/// <c>Chaos.Shell.Program</c> into App.g.i.cs, and a class of ours by that name
/// collides with it. The csproj also names this type as StartupObject so there
/// is never any ambiguity about which Main runs.
///
/// This owns Main because single-instance redirection has to happen BEFORE any
/// XAML is created: a second launch must reach the running shell rather than
/// build a second one and throw it away.
///
/// The Windows App SDK bootstrapper is injected as a module initializer for an
/// unpackaged WinExe, so it has already run by the time this executes. Do not
/// call Bootstrap.TryInitialize here as well.
/// </summary>
public static class ShellEntryPoint
{
    [STAThread]
    public static int Main(string[] rawArgs)
    {
        var options = ShellCommandLine.Parse(rawArgs);

        // One shell per user session. The key is fixed rather than derived from
        // the host URL: two shells pointed at different gateways would still
        // fight over one tray icon, and an operator seeing two CHAOS icons has
        // no way to tell which is authoritative.
        var instance = AppInstance.FindOrRegisterForKey("Chaos.Shell.SingleInstance");
        if (!instance.IsCurrent)
        {
            // Hand our activation to the running shell and exit quietly.
            var activation = AppInstance.GetCurrent().GetActivatedEventArgs();
            instance.RedirectActivationToAsync(activation).AsTask().GetAwaiter().GetResult();
            return 0;
        }

        global::WinRT.ComWrappersSupport.InitializeComWrappers();

        // Published before Application.Start so that App's parameterless
        // constructor — the one the XAML-generated Main would use — starts
        // against the same gateway and activation this entry point resolved,
        // rather than against defaults.
        App.PendingOptions = options;
        App.PendingInstance = instance;

        // The callback parameter is deliberately NOT named "_": that would make
        // any discard assignment inside the body assign to the parameter.
        Microsoft.UI.Xaml.Application.Start(callbackParams =>
        {
            var context = new DispatcherQueueSynchronizationContext(
                DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);

            // Application.Start owns the instance's lifetime; it must not be
            // assigned to anything here.
            _ = new App(options, instance);
        });

        return 0;
    }
}
