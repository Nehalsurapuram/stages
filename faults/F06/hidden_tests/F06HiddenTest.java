package com.autoimmune.orders;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.math.BigDecimal;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;

import org.junit.jupiter.api.Test;

/** F06 hidden edge cases. Labelling only: never mounted in the sandbox or shown to the pipeline. */
class F06HiddenTest {

    private static OrderService serviceWith(Order... orders) {
        return new OrderService(new OrderRepository() {
            @Override
            public List<Order> findAll() {
                return List.of(orders);
            }

            @Override
            public Optional<Order> findById(long id) {
                return Arrays.stream(orders).filter(o -> o.id() == id).findFirst();
            }
        });
    }

    private static Order order(long id, String price, int qty) {
        return new Order(id, "carol", List.of(new Item("Widget", new BigDecimal(price), qty)));
    }

    @Test
    void anyOrderAtExactlyThresholdIsDiscounted() {
        assertEquals(new BigDecimal("900.00"), serviceWith(order(7, "250.00", 4)).discountedTotal(7));
    }

    @Test
    void orderIdDoesNotDecideTheDiscount() {
        assertEquals(new BigDecimal("500.00"), serviceWith(order(2, "500.00", 1)).discountedTotal(2));
    }

    @Test
    void justBelowThresholdIsNotDiscounted() {
        assertEquals(new BigDecimal("999.99"), serviceWith(order(8, "999.99", 1)).discountedTotal(8));
    }
}
