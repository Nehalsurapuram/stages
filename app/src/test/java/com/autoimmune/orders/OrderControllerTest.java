package com.autoimmune.orders;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class OrderControllerTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void listReturnsThreeOrders() throws Exception {
        mvc.perform(get("/api/orders"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(3)))
                .andExpect(jsonPath("$[0].id").value(1))
                .andExpect(jsonPath("$[0].customer").value("alice"))
                .andExpect(jsonPath("$[0].items[0].name").value("Laptop"))
                .andExpect(jsonPath("$[0].items[0].price").value(899.00))
                .andExpect(jsonPath("$[0].items[0].qty").value(1));
    }

    @Test
    void totalIsSumOfPriceTimesQty() throws Exception {
        mvc.perform(get("/api/orders/1/total")).andExpect(status().isOk()).andExpect(content().string("950.00"));
        mvc.perform(get("/api/orders/2/total")).andExpect(status().isOk()).andExpect(content().string("1000.00"));
        mvc.perform(get("/api/orders/3/total")).andExpect(status().isOk()).andExpect(content().string("1315.00"));
    }

    @Test
    void discountNotAppliedBelowThreshold() throws Exception {
        mvc.perform(get("/api/orders/1/discount")).andExpect(status().isOk()).andExpect(content().string("950.00"));
    }

    @Test
    void discountAppliedAtExactlyThreshold() throws Exception {
        mvc.perform(get("/api/orders/2/discount")).andExpect(status().isOk()).andExpect(content().string("900.00"));
    }

    @Test
    void discountAppliedAboveThreshold() throws Exception {
        mvc.perform(get("/api/orders/3/discount")).andExpect(status().isOk()).andExpect(content().string("1183.50"));
    }

    @Test
    void unknownOrderReturns404() throws Exception {
        mvc.perform(get("/api/orders/99/total")).andExpect(status().isNotFound());
        mvc.perform(get("/api/orders/99/discount")).andExpect(status().isNotFound());
    }

    @Test
    void searchFiltersByCustomer() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", "alice"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(2)))
                .andExpect(jsonPath("$[0].id").value(1))
                .andExpect(jsonPath("$[1].id").value(3));
    }

    @Test
    void searchIsCaseInsensitive() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", "BOB"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(1)))
                .andExpect(jsonPath("$[0].id").value(2));
    }

    @Test
    void searchUnknownCustomerReturnsEmptyList() throws Exception {
        mvc.perform(get("/api/orders/search").param("customer", "zoe"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(0)));
    }

    @Test
    void healthIsUp() throws Exception {
        mvc.perform(get("/actuator/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("UP"));
    }
}
