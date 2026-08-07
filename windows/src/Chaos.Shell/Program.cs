using System.Runtime.InteropServices;
using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.Windows.AppLifecycle;

namespace Chaos.Shell;

/// <summary>
/// Entry point. Owns <c>Main</c> rather than letting the XAML generator produce
/// one (see DISABLE_XAML_GENERATED_MAIN in the csproj), because single-instance
/// redirection has to happen BEFORE any XAML is created: a second launch must
/// reach the running shell, not build a second one and then throw it away.
///
/// The Windows App SDK bootstrapper is injected as a module initializer for an
/// unpackaged WinExe, so it has already run by the time this method executes.
/// Do not call Bootstrap.TryInitialize here as well.
/// </summary>
public static class Program
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

        Microsoft.UI.Xaml.Application.Start(_ =>
        {
            var context = new DispatcherQueueSynchronizationContext(
                DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            _ = new App(options, instance);
        });

        return 0;
    }
}
