package com.autoimmune.orders;

import static org.hamcrest.Matchers.containsString;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.actuate.observability.AutoConfigureObservability;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/** Tests switch metrics export off by default; @AutoConfigureObservability turns it back on for this class. */
@SpringBootTest
@AutoConfigureMockMvc
@AutoConfigureObservability
class MetricsEndpointTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void prometheusEndpointExposesRequestHistogram() throws Exception {
        mvc.perform(get("/api/orders/1/total")).andExpect(status().isOk());
        mvc.perform(get("/actuator/prometheus"))
                .andExpect(status().isOk())
                .andExpect(content().string(containsString("http_server_requests_seconds_bucket{")))
                .andExpect(content().string(containsString("uri=\"/api/orders/{id}/total\"")));
    }
}
