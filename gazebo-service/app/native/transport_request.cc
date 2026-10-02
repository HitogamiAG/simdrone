// Harmonic's Python RequestRaw binding holds the GIL while waiting for a reply.
// The shared Transport receiver also invokes Python subscribers: release the
// GIL here so a subscriber cannot block the service reply behind its callback.
#include <gz/transport/Node.hh>
#include <pybind11/pybind11.h>
#include <string>

namespace py = pybind11;

PYBIND11_MODULE(_transport_request, module)
{
  module.def("request_raw", [](gz::transport::Node &node,
      const std::string &topic, const std::string &request,
      const std::string &requestType, const std::string &responseType,
      unsigned int timeout)
  {
    std::string response;
    bool result = false;
    bool executed = false;
    {
      py::gil_scoped_release release;
      executed = node.RequestRaw(topic, request, requestType, responseType,
                                 timeout, response, result);
    }
    return py::make_tuple(executed && result, py::bytes(response));
  });
  module.def("unsubscribe", [](gz::transport::Node &node,
                              const std::string &topic)
  {
    py::gil_scoped_release release;
    return node.Unsubscribe(topic);
  });
}
