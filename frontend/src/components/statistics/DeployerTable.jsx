export default function DeployerTable({ deployers = [] }) {
  if (deployers.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 text-sm">
        Aucun déployeur pour cette période
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-gray-50 border-b border-gray-200">
            <th className="px-4 py-3 text-left font-semibold text-gray-600">Déployeur</th>
            <th className="px-4 py-3 text-left font-semibold text-gray-600">Email</th>
            <th className="px-4 py-3 text-center font-semibold text-gray-600">Total</th>
            <th className="px-4 py-3 text-center font-semibold text-gray-600">Avec IA</th>
            <th className="px-4 py-3 text-center font-semibold text-gray-600">Sans IA</th>
            <th className="px-4 py-3 text-left font-semibold text-gray-600">Sites</th>
            <th className="px-4 py-3 text-left font-semibold text-gray-600">Modes</th>
            <th className="px-4 py-3 text-right font-semibold text-gray-600">Coût IA</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {deployers.map((d, i) => (
            <tr key={d.email || i} className="hover:bg-gray-50 transition-colors">
              <td className="px-4 py-3 font-medium text-gray-900">{d.name}</td>
              <td className="px-4 py-3 text-gray-500">{d.email}</td>
              <td className="px-4 py-3 text-center font-semibold text-gray-900">{d.total}</td>
              <td className="px-4 py-3 text-center">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                  {d.with_ai}
                </span>
              </td>
              <td className="px-4 py-3 text-center">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-700">
                  {d.without_ai}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-600">
                <div className="flex flex-wrap gap-1">
                  {(d.websites || []).map((site) => (
                    <span
                      key={site}
                      className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-purple-50 text-purple-700"
                    >
                      {site}
                    </span>
                  ))}
                </div>
              </td>
              <td className="px-4 py-3 text-gray-600">
                <div className="flex flex-wrap gap-1">
                  {(d.modes || []).map((mode) => (
                    <span
                      key={mode}
                      className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-orange-50 text-orange-700 capitalize"
                    >
                      {mode}
                    </span>
                  ))}
                </div>
              </td>
              <td className="px-4 py-3 text-right text-gray-600 font-mono text-xs">
                {d.ai_cost}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
